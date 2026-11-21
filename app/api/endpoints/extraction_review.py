from hashlib import sha256
from uuid import uuid4

import pymupdf as fitz

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.blocking import run_blocking_safely
from app.core.database import get_postgres_session
from app.core.processing_coordinator import processing_coordinator
from app.models.document_upload import PageTextCorrection
from app.services.extraction_review import Correction, CorrectionConflict, inspect_page
from app.services.graph_retry_policy import GraphRetryNotDue
from app.services.parser_service import ExtractedPage, ParserService
from app.services.resource_budget_service import ResourceBudgetExceeded
from app.services.security_service import demo_access_service
from app.services.storage_service import ObjectNotFoundError, ObjectStorageError, storage_service
from app.services.upload_service import UploadService

router = APIRouter(prefix="/api/v1/ingest/uploads", tags=["extraction review"])
uploads = UploadService()


def reviewed_chunks(text, page, course_id, upload_id, name):
    return ParserService().chunk_pages([ExtractedPage(page, text, "reviewer_corrected")],
                                       course_id, upload_id, name, max_chunks=settings.max_chunks_per_document)


class PageEdit(BaseModel):
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    task_id: str = Field(min_length=1, max_length=64)
    revision: int = Field(ge=0)
    text: str = Field(min_length=1, max_length=60000)


async def _source(db, upload_id):
    record = await uploads.get_upload(db, upload_id)
    if record is None:
        raise HTTPException(404, "Upload not found.")
    if uploads.is_deleting(record):
        raise HTTPException(409, "Document removal is in progress.")
    try:
        return record, await run_blocking_safely(storage_service.get_bytes, record.storage_key)
    except ObjectNotFoundError as exc:
        raise HTTPException(404, "The original PDF is unavailable.") from exc
    except ObjectStorageError as exc:
        raise HTTPException(503, "Document storage is temporarily unavailable.") from exc


@router.get("/{upload_id}/extraction/{page_number}")
async def review_page(upload_id: str, page_number: int = Path(ge=1), db: AsyncSession = Depends(get_postgres_session)):
    record, source = await _source(db, upload_id)
    try:
        data = await run_blocking_safely(inspect_page, source, page_number, ParserService())
    except IndexError as exc:
        raise HTTPException(404, str(exc)) from exc
    row = await db.get(PageTextCorrection, (upload_id, page_number))
    if row and row.source_sha256 != data["source_sha256"]:
        raise HTTPException(409, "The stored PDF changed after review.")
    chunks = reviewed_chunks(row.corrected_text, page_number, record.course_uuid, upload_id, record.original_filename) if row else []
    return {**data, "publication_enabled": settings.graph_projection_fencing_enabled,
            "reviewed_chunks": [{"id": chunk.id, "text": chunk.text} for chunk in chunks],
            "revision": row.revision if row else 0, "corrected_text": row.corrected_text if row else None,
            "task_id": record.task_id, "can_correct": record.stage in uploads.DELETABLE_STAGES
            and demo_access_service.enabled and record.content_hash == data["source_sha256"]}


@router.post("/{upload_id}/extraction/{page_number}", status_code=202)
async def correct_page(upload_id: str, edit: PageEdit, page_number: int = Path(ge=1),
                       db: AsyncSession = Depends(get_postgres_session)):
    if not demo_access_service.enabled:
        raise HTTPException(403, "Configure reviewer authentication to save corrections.")
    corrected = edit.text.strip()
    if not corrected or "\x00" in corrected:
        raise HTTPException(422, "Enter nonempty page text without null characters.")
    record, source = await _source(db, upload_id)
    with fitz.open(stream=source, filetype="pdf") as document:
        if page_number > len(document):
            raise HTTPException(404, "Page number is outside this PDF.")
    if sha256(source).hexdigest() != edit.source_sha256:
        raise HTTPException(409, "The source PDF changed. Reload the review.")
    task_id = str(uuid4())
    try:
        chunks = reviewed_chunks(corrected, page_number, record.course_uuid, upload_id, record.original_filename)
        updated = await uploads.retry_upload(db, upload_id, task_id, correction=Correction(
            page_number, edit.source_sha256, edit.task_id, edit.revision, corrected),
            publish_correction=settings.graph_projection_fencing_enabled)
        if updated is None:
            raise CorrectionConflict("The document is no longer eligible for review.")
    except CorrectionConflict as exc:
        await db.rollback()
        raise HTTPException(409, str(exc)) from exc
    except (ResourceBudgetExceeded, GraphRetryNotDue) as exc:
        await db.rollback()
        raise HTTPException(429, str(exc)) from exc
    if settings.graph_projection_fencing_enabled:
        await processing_coordinator.submit(upload_id, task_id)
    return {"task_id": updated.task_id, "revision": edit.revision + 1, "rebuilt_chunks": len(chunks),
            "status": "queued" if settings.graph_projection_fencing_enabled else "saved_for_review"}
