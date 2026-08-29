"""API endpoints for document ingestion and upload tracking."""

import asyncio
import hashlib
from urllib.parse import quote
from uuid import uuid4

import pymupdf as fitz
from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, UploadFile, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from app.core.database import get_postgres_session
from app.core.config import settings
from app.schemas.ingest import CourseSummaryResponse, IngestResponse, UploadStatusResponse
from app.services.upload_service import UploadService
from app.services.course_service import CourseService
from app.services.storage_service import (
    ObjectNotFoundError,
    ObjectStorageError,
    StorageService,
    storage_service,
)
from app.core.processing import FailureCategory
from app.core.processing_coordinator import EnqueueDisposition, processing_coordinator
from app.services.document_processing_service import document_processing_service

router = APIRouter(prefix="/api/v1/ingest", tags=["ingest"])

upload_service = UploadService()
course_service = CourseService()
MAX_UPLOAD_BYTES = settings.max_pdf_size_mb * 1024 * 1024
MAX_UPLOAD_DETAIL = f"PDF files must be {settings.max_pdf_size_mb} MB or smaller."

@router.post(
    "/upload",
    response_model=IngestResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload a PDF syllabus for processing",
)
async def upload_document(
    course_id: str = Form(..., description="Identifier for the course/document"),
    file: UploadFile = File(..., description="The PDF syllabus to ingest"),
    db: AsyncSession = Depends(get_postgres_session),
) -> IngestResponse:
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported.")
    if file.content_type not in {"application/pdf", "application/x-pdf"}:
        raise HTTPException(status_code=400, detail="The selected file is not a valid PDF.")
    if file.size is not None and file.size > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=MAX_UPLOAD_DETAIL)
    course_id = course_id.strip()
    if not course_id:
        raise HTTPException(status_code=400, detail="course_id cannot be empty.")

    content = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=MAX_UPLOAD_DETAIL)
    if not content:
        raise HTTPException(status_code=400, detail="The selected PDF is empty.")
    if not content.startswith(b"%PDF-"):
        raise HTTPException(status_code=400, detail="The selected file has an invalid PDF signature.")
    try:
        with fitz.open(stream=content, filetype="pdf") as document:
            if document.needs_pass:
                raise HTTPException(
                    status_code=400,
                    detail="Password-protected PDFs are not supported.",
                )
            if document.page_count == 0:
                raise HTTPException(status_code=400, detail="The PDF contains no pages.")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail="The PDF is malformed or unreadable.") from exc

    content_hash = hashlib.sha256(content).hexdigest()
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
        {"key": "conceptgraph:pdf-admission"},
    )
    course = await course_service.get_or_create(db, course_id)
    duplicate = await upload_service.find_duplicate(db, course.id, content_hash)
    if duplicate is not None:
        return _ingest_response(
            duplicate,
            message="This PDF already exists in the course. The existing document was returned.",
            duplicate=True,
        )
    if await upload_service.count_uploads(db) >= settings.max_pdfs_per_installation:
        raise HTTPException(
            status_code=429,
            detail=(
                "This installation has reached its configured PDF limit. "
                "Remove an eligible document before uploading another."
            ),
        )

    upload_id = str(uuid4())
    task_id = str(uuid4())
    storage_key = StorageService.object_key(course.id, content_hash)
    try:
        await asyncio.to_thread(storage_service.put_pdf, storage_key, content)
    except ObjectStorageError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=503,
            detail="Document storage is temporarily unavailable. Please try again.",
        ) from exc

    try:
        await upload_service.create_upload(
            db,
            upload_id=upload_id,
            task_id=task_id,
            course=course,
            content_hash=content_hash,
            original_filename=file.filename,
            storage_key=storage_key,
        )
    except Exception as exc:
        await db.rollback()
        # The commit result can be ambiguous after a connection failure. Keep
        # the content-addressed object so a possibly committed row never loses
        # its source; a later reconciliation job can remove true orphans.
        raise HTTPException(status_code=503, detail="Document tracking is temporarily unavailable.") from exc

    disposition = await processing_coordinator.submit(upload_id, task_id)
    message = (
        "Background processing has started."
        if disposition != EnqueueDisposition.DEFERRED
        else "The upload was saved and will start when processing capacity is available."
    )

    return IngestResponse(
        message=message,
        task_id=task_id,
        upload_id=upload_id,
        course_id=course.id,
        course_name=course.display_name,
        original_filename=file.filename,
        preview_url=f"/api/v1/ingest/uploads/{upload_id}/preview",
    )


@router.get("/status/{task_id}", response_model=UploadStatusResponse)
async def get_upload_status(
    task_id: str,
    db: AsyncSession = Depends(get_postgres_session),
) -> UploadStatusResponse:
    record = await upload_service.get_upload_by_task_id(db, task_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Upload not found.")

    return UploadStatusResponse(
        upload_id=record.upload_id,
        task_id=record.task_id,
        course_id=record.course_uuid or record.course_id,
        original_filename=record.original_filename,
        course_name=record.course_id,
        status=record.status,
        stage=record.stage,
        failure_category=record.failure_category,
        retryable=record.retryable,
        attempt_count=record.attempt_count,
        last_attempted_at=record.last_attempted_at,
        last_heartbeat_at=record.last_heartbeat_at,
        processed_chunk_count=record.processed_chunk_count,
        graph_node_count=record.graph_node_count,
        graph_edge_count=record.graph_edge_count,
        graph_status=record.graph_status,
        error_message=record.error_message,
        result_json=record.result_json,
        created_at=record.created_at,
        updated_at=record.updated_at,
        started_at=record.started_at,
        completed_at=record.completed_at,
        preview_url=f"/api/v1/ingest/uploads/{record.upload_id}/preview",
    )


@router.get("/uploads", response_model=list[UploadStatusResponse])
async def list_uploads(
    limit: int = 25,
    db: AsyncSession = Depends(get_postgres_session),
) -> list[UploadStatusResponse]:
    records = await upload_service.list_uploads(db, limit=100)
    grouped: dict[tuple[str, str], object] = {}
    rank = {"active": 0, "ready": 1, "failed": 2, "cancelled": 3}
    for record in records:
        key = (record.course_uuid or record.course_id, record.content_hash or record.upload_id)
        current = grouped.get(key)
        if current is None or rank.get(record.status, 9) < rank.get(current.status, 9):
            grouped[key] = record
    records = list(grouped.values())[: max(1, min(limit, 100))]
    return [
        UploadStatusResponse(
            upload_id=record.upload_id,
            task_id=record.task_id,
            course_id=record.course_uuid or record.course_id,
            course_name=record.course_id,
            original_filename=record.original_filename,
            status=record.status,
            stage=record.stage,
            failure_category=record.failure_category,
            retryable=record.retryable,
            attempt_count=record.attempt_count,
            last_attempted_at=record.last_attempted_at,
            last_heartbeat_at=record.last_heartbeat_at,
            processed_chunk_count=record.processed_chunk_count,
            graph_node_count=record.graph_node_count,
            graph_edge_count=record.graph_edge_count,
            graph_status=record.graph_status,
            error_message=record.error_message,
            result_json=record.result_json,
            created_at=record.created_at,
            updated_at=record.updated_at,
            started_at=record.started_at,
            completed_at=record.completed_at,
            preview_url=f"/api/v1/ingest/uploads/{record.upload_id}/preview",
        )
        for record in records
    ]


@router.get("/courses", response_model=list[CourseSummaryResponse])
async def list_courses(
    db: AsyncSession = Depends(get_postgres_session),
) -> list[CourseSummaryResponse]:
    summaries = await course_service.list_summaries(db)
    return [
        CourseSummaryResponse(
            course_id=summary.course.id,
            course_name=summary.course.display_name,
            total_documents=summary.total_documents,
            active_documents=summary.active_documents,
            ready_documents=summary.ready_documents,
            failed_documents=summary.failed_documents,
            processed_chunk_count=summary.processed_chunk_count,
            graph_node_count=summary.graph_node_count,
            graph_edge_count=summary.graph_edge_count,
            graph_status=summary.graph_status,
            last_updated_at=summary.last_updated_at,
            historical_records=summary.historical_records,
            duplicate_records=summary.duplicate_records,
        )
        for summary in summaries
    ]


@router.post("/uploads/{upload_id}/retry", response_model=IngestResponse)
async def retry_upload(
    upload_id: str,
    db: AsyncSession = Depends(get_postgres_session),
) -> IngestResponse:
    existing = await upload_service.get_upload(db, upload_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Upload not found.")
    if existing.stage != "FAILED" or not existing.retryable:
        raise HTTPException(status_code=409, detail="Only failed uploads can be retried.")

    task_id = str(uuid4())
    record = await upload_service.retry_upload(db, upload_id, task_id)
    if record is None:
        raise HTTPException(status_code=409, detail="Only failed uploads can be retried.")

    try:
        source_exists = await asyncio.to_thread(storage_service.exists, record.storage_key)
    except ObjectStorageError as exc:
        await upload_service.mark_failed(
            db,
            upload_id,
            task_id,
            "Document storage is temporarily unavailable. Please retry when it is restored.",
            FailureCategory.DATABASE_ERROR,
            True,
        )
        raise HTTPException(status_code=503, detail="Document storage is temporarily unavailable.") from exc
    if not source_exists:
        await upload_service.mark_failed(
            db,
            upload_id,
            task_id,
            "The stored PDF is no longer available. Upload the document again.",
            FailureCategory.DOCUMENT_ERROR,
            False,
        )
        raise HTTPException(status_code=404, detail="The stored PDF is no longer available.")

    disposition = await processing_coordinator.submit(upload_id, record.task_id)
    message = (
        "Document processing has been queued again."
        if disposition != EnqueueDisposition.DEFERRED
        else "The retry was saved and will start when processing capacity is available."
    )
    return IngestResponse(
        message=message,
        task_id=record.task_id,
        upload_id=record.upload_id,
        course_id=record.course_uuid or record.course_id,
        course_name=record.course_id,
        original_filename=record.original_filename,
        preview_url=f"/api/v1/ingest/uploads/{record.upload_id}/preview",
    )


@router.get("/uploads/{upload_id}/preview")
async def preview_upload(
    upload_id: str,
    range_header: str | None = Header(default=None, alias="Range"),
    db: AsyncSession = Depends(get_postgres_session),
) -> Response:
    record = await upload_service.get_upload(db, upload_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Upload not found.")

    try:
        content = await asyncio.to_thread(storage_service.get_bytes, record.storage_key)
    except ObjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Stored PDF is no longer available.")
    except ObjectStorageError as exc:
        raise HTTPException(status_code=503, detail="Document storage is temporarily unavailable.") from exc

    return build_pdf_response(content, record.original_filename, range_header)


@router.delete("/uploads/{upload_id}", status_code=204)
async def remove_upload(
    upload_id: str,
    db: AsyncSession = Depends(get_postgres_session),
) -> None:
    existing = await upload_service.get_upload(db, upload_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Upload not found.")
    if existing.stage not in upload_service.DELETABLE_STAGES:
        raise HTTPException(
            status_code=409,
            detail="Only ready or failed document records can be removed.",
        )

    record = await upload_service.lock_for_deletion(db, upload_id)
    if record is None:
        raise HTTPException(
            status_code=409,
            detail="The document state changed before removal.",
        )

    course_scope = record.course_uuid or record.course_id
    try:
        await document_processing_service.ingestion_service.cleanup_upload(
            record.upload_id,
            course_scope,
        )
    except Exception as exc:
        await db.rollback()
        raise HTTPException(
            status_code=503,
            detail="Indexed document artifacts are temporarily unavailable.",
        ) from exc

    shared_object = await upload_service.storage_key_is_shared(
        db,
        record.storage_key,
        record.upload_id,
    )
    if not shared_object:
        try:
            await asyncio.to_thread(storage_service.delete, record.storage_key)
        except ObjectStorageError as exc:
            await db.rollback()
            raise HTTPException(status_code=503, detail="Document storage is temporarily unavailable.") from exc
    deleted = await upload_service.delete_document(db, upload_id)
    if deleted is None:
        await db.rollback()
        raise HTTPException(status_code=409, detail="The document state changed before removal.")


def build_pdf_response(content: bytes, filename: str, range_header: str | None) -> Response:
    total = len(content)
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Disposition": f"inline; filename*=UTF-8''{quote(filename)}",
    }
    if not range_header:
        headers["Content-Length"] = str(total)
        return Response(content=content, media_type="application/pdf", headers=headers)

    try:
        unit, requested = range_header.strip().split("=", 1)
        if unit != "bytes" or "," in requested:
            raise ValueError
        start_text, end_text = requested.split("-", 1)
        if start_text:
            start = int(start_text)
            end = min(int(end_text), total - 1) if end_text else total - 1
        else:
            suffix_length = int(end_text)
            if suffix_length <= 0:
                raise ValueError
            start = max(total - suffix_length, 0)
            end = total - 1
        if start < 0 or start >= total or end < start:
            raise ValueError
    except (TypeError, ValueError):
        return Response(
            status_code=416,
            headers={"Content-Range": f"bytes */{total}", "Accept-Ranges": "bytes"},
        )

    partial = content[start : end + 1]
    headers.update(
        {
            "Content-Range": f"bytes {start}-{end}/{total}",
            "Content-Length": str(len(partial)),
        }
    )
    return Response(
        content=partial,
        status_code=206,
        media_type="application/pdf",
        headers=headers,
    )


_pdf_response = build_pdf_response


def _ingest_response(record, *, message: str, duplicate: bool) -> IngestResponse:
    return IngestResponse(
        message=message,
        task_id=record.task_id,
        upload_id=record.upload_id,
        course_id=record.course_uuid or record.course_id,
        course_name=record.course_id,
        original_filename=record.original_filename,
        status=record.stage,
        duplicate=duplicate,
        preview_url=f"/api/v1/ingest/uploads/{record.upload_id}/preview",
    )
