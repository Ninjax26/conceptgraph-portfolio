"""Protected, offline revision comparison of two explicitly selected course PDFs."""
import asyncio
from dataclasses import asdict
from hashlib import sha256
import json

import pymupdf
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import and_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.blocking import run_blocking_safely
from app.core.database import get_postgres_session
from app.models.document_upload import DocumentUpload
from app.models.revision_pair import RevisionPair
from app.services.course_service import CourseNotFoundError, CourseNotReadyError, CourseService
from app.services.revision_service import ComparisonUnavailable, compare_sections, extract_sections
from app.services.storage_service import ObjectNotFoundError, ObjectStorageError, storage_service
from app.services.upload_service import UploadService

router = APIRouter(prefix="/api/v1/courses", tags=["revisions"])
_comparison_slot = asyncio.Semaphore(1)


class AlignmentReview(BaseModel):
    earlier_section: str = Field(min_length=1, max_length=24)
    later_section: str | None = Field(default=None, max_length=24)
    version: int = Field(ge=1)
    evidence_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class ComparisonRequest(BaseModel):
    earlier_id: str = Field(min_length=1, max_length=64)
    later_id: str = Field(min_length=1, max_length=64)
    review: AlignmentReview | None = None


async def _eligible(db, course_id):
    try:
        context = await CourseService().get_ready_context(db, course_id)
    except CourseNotFoundError as exc:
        raise HTTPException(404, "Course not found.") from exc
    except CourseNotReadyError:
        return {}
    return {doc.upload_id: doc for doc in context.documents if doc.status == "ready"}


def _metadata(doc):
    return {"id": doc.upload_id, "filename": doc.original_filename, "created_at": doc.created_at}


@router.get("/{course_id}/revisions")
async def revision_options(course_id: str, db: AsyncSession = Depends(get_postgres_session)):
    eligible = await _eligible(db, course_id)
    pairs = []
    if len(eligible) >= 2:
        pairs = (await db.execute(select(RevisionPair.earlier_id, RevisionPair.later_id)
                                  .where(RevisionPair.earlier_id.in_(eligible),
                                         RevisionPair.later_id.in_(eligible))
                                  .order_by(RevisionPair.earlier_id, RevisionPair.later_id))).all()
    return {"documents": [_metadata(doc) for doc in eligible.values()],
            "pairs": [{"earlier_id": old, "later_id": new} for old, new in pairs]}


def _read_pair(sources):
    sections, warnings = [], []
    digest = sha256()
    for key, content_hash in sources:
        content = storage_service.get_bytes(key)
        extracted, uncertain = extract_sections(content, content_hash)
        digest.update(sha256(content).digest())
        digest.update(sha256(json.dumps([asdict(x) for x in extracted], sort_keys=True).encode()).digest())
        sections.append(extracted)
        warnings.append(uncertain)
    return sections, warnings, digest.hexdigest()


@router.post("/{course_id}/revisions/compare")
async def compare_revisions(course_id: str, request: ComparisonRequest,
                            db: AsyncSession = Depends(get_postgres_session)):
    if request.earlier_id == request.later_id:
        raise HTTPException(422, "Choose two different uploads.")
    eligible = await _eligible(db, course_id)
    ids = (request.earlier_id, request.later_id)
    if not all(key in eligible for key in ids):
        raise HTTPException(404, "Both uploads must be available and ready in this course.")
    docs = [eligible[key] for key in ids]
    identity = [(d.course_uuid, d.storage_key, d.content_hash, d.task_id) for d in docs]
    metadata = [_metadata(doc) for doc in docs]
    await db.rollback()  # Release the read transaction while reading and comparing source files.
    if _comparison_slot.locked():
        raise HTTPException(429, "Another revision comparison is running. Try again shortly.")
    async with _comparison_slot:
        try:
            sections, warnings, digest = await run_blocking_safely(
                _read_pair, [(storage_key, content_hash) for _, storage_key, content_hash, _ in identity])
        except ObjectNotFoundError as exc:
            raise HTTPException(404, "A source PDF is no longer available.") from exc
        except ObjectStorageError as exc:
            raise HTTPException(503, "Document storage is temporarily unavailable.") from exc
        except (ComparisonUnavailable, pymupdf.FileDataError) as exc:
            raise HTTPException(422, str(exc)) from exc

        # Serialize with deletion/reprocessing and verify the same eligible uploads still exist.
        result = await db.execute(select(DocumentUpload).where(DocumentUpload.upload_id.in_(ids))
                                  .order_by(DocumentUpload.upload_id).with_for_update())
        current = {d.upload_id: d for d in result.scalars()}
        for key, captured in zip(ids, identity):
            doc = current.get(key)
            if (not doc or doc.stage != "READY" or doc.status != "ready" or not doc.processed_chunk_count
                    or UploadService.is_deleting(doc)
                    or (doc.course_uuid, doc.storage_key, doc.content_hash, doc.task_id) != captured):
                raise HTTPException(409, "An upload changed during comparison. Reopen the comparison.")

        pair_key = and_(RevisionPair.earlier_id == ids[0], RevisionPair.later_id == ids[1])
        await db.execute(insert(RevisionPair).values(earlier_id=ids[0], later_id=ids[1],
                         evidence_digest=digest, reviewed_matches={}, version=1).on_conflict_do_nothing())
        pair = (await db.execute(select(RevisionPair).where(pair_key).with_for_update())).scalar_one()
        review = request.review
        if review and (review.version != pair.version or review.evidence_digest != digest
                       or pair.evidence_digest != digest):
            raise HTTPException(409, "This comparison changed. Reopen it before reviewing.")
        decisions = dict(pair.reviewed_matches) if pair.evidence_digest == digest else {}
        if review:
            if review.earlier_section in decisions:
                raise HTTPException(409, "This section already has a saved review.")
            pending_result = compare_sections(*sections, decisions)
            pending = {item["earlier"]["id"] for item in pending_result["pending"]}
            if review.earlier_section not in pending:
                raise HTTPException(422, "Only unresolved sections can be reviewed.")
            decisions[review.earlier_section] = review.later_section
        try:
            comparison = compare_sections(*sections, decisions)
        except ComparisonUnavailable as exc:
            raise HTTPException(422, str(exc)) from exc
        if review or pair.evidence_digest != digest:
            pair.reviewed_matches = decisions
            pair.evidence_digest = digest
            pair.version += 1
        version = pair.version
        await db.commit()

    return {"earlier": metadata[0], "later": metadata[1], "version": version,
            "evidence_digest": digest, "earlier_review_pages": warnings[0],
            "later_review_pages": warnings[1], **comparison}
