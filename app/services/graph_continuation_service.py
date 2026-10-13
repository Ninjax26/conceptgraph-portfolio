"""Durable graph-job admission/claim/checkpoint foundation.

Not connected to the coordinator or any API route yet. Opt-in READY completion
can stage snapshots atomically, but this module performs
PostgreSQL operations only; it never generates or projects a graph. A future
graph-only worker must publish under the document/job fence before activation.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json
from uuid import uuid4

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.processing import MAX_GRAPH_PROCESSING_ATTEMPTS
from app.models.document_upload import DocumentUpload, GraphContinuationJob, GraphExtractionCheckpoint
from app.services.graph_retry_policy import retry_not_before, retry_wait_seconds
from app.services.ingestion_service import GraphSectionBatch, IngestionService
from app.services.parser_service import DocumentChunk
from app.services.upload_service import UploadService
from app.schemas.extraction import GraphExtractionResponse

MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024
SNAPSHOT_VERSION = 1


def snapshot_digest(payload: dict) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
    if len(encoded) > MAX_SNAPSHOT_BYTES:
        raise ValueError("Graph continuation snapshot exceeds its storage bound.")
    return hashlib.sha256(encoded).hexdigest()


def snapshot_chunks(payload: dict, record: DocumentUpload) -> tuple[DocumentChunk, ...]:
    identity = {
        "version": SNAPSHOT_VERSION, "upload_id": record.upload_id,
        "task_id": record.task_id, "course_uuid": record.course_uuid,
        "content_hash": record.content_hash, "storage_key": record.storage_key,
        "document_name": record.original_filename,
    }
    if (not isinstance(payload, dict) or set(payload) != {*identity, "chunks"}
        or type(payload.get("version")) is not int
        or any(payload.get(key) != value for key, value in identity.items())):
        raise ValueError("Graph snapshot belongs to another document execution.")
    rows = payload.get("chunks")
    if not isinstance(rows, list) or not 1 <= len(rows) <= settings.max_chunks_per_document:
        raise ValueError("Graph snapshot has an invalid chunk count.")
    chunks = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"id", "text", "metadata"}:
            raise ValueError("Invalid graph snapshot chunk.")
        chunk_id, text, metadata = row["id"], row["text"], row["metadata"]
        if not isinstance(chunk_id, str) or not chunk_id or chunk_id in seen or not isinstance(text, str) or not text.strip():
            raise ValueError("Graph snapshot has invalid identifiers or text.")
        if not isinstance(metadata, dict) or any(metadata.get(key) != value for key, value in {
            "upload_id": record.upload_id, "document_id": record.course_uuid,
            "execution_token": record.task_id, "document_name": record.original_filename,
            "chunk_id": chunk_id,
        }.items()):
            raise ValueError("Graph snapshot contains foreign source metadata.")
        page = metadata.get("page_number")
        if type(page) is not int or page < 1 or (record.page_count is not None and page > record.page_count):
            raise ValueError("Graph snapshot has an invalid source page.")
        seen.add(chunk_id)
        chunks.append(DocumentChunk(id=chunk_id, text=text, metadata=dict(metadata)))
    snapshot_digest(payload)
    return tuple(chunks)


def create_snapshot(record: DocumentUpload, chunks: Sequence[DocumentChunk]) -> dict:
    payload = {
        "version": SNAPSHOT_VERSION, "upload_id": record.upload_id, "task_id": record.task_id,
        "course_uuid": record.course_uuid, "content_hash": record.content_hash,
        "storage_key": record.storage_key, "document_name": record.original_filename,
        "chunks": [{"id": chunk.id, "text": chunk.text, "metadata": dict(chunk.metadata)} for chunk in chunks],
    }
    snapshot_chunks(payload, record)
    return payload


def extraction_contract(chunks: Sequence[DocumentChunk]) -> str:
    keys = [batch.batch_key for batch in IngestionService._graph_section_batches(chunks, batch_size=settings.graph_batch_size)]
    return snapshot_digest({"keys": keys, "global_linking_enabled": settings.graph_global_linking_enabled})


@dataclass(frozen=True, slots=True)
class GraphJobClaim:
    job_id: str
    upload_id: str
    document_task_id: str
    claim_token: str
    lease_owner: str
    chunks: tuple[DocumentChunk, ...]


class GraphContinuationService:
    """Lock order is document, then job, then checkpoint. Never the reverse."""

    @staticmethod
    async def _document(session: AsyncSession, upload_id: str) -> DocumentUpload | None:
        return (await session.execute(select(DocumentUpload).where(DocumentUpload.upload_id == upload_id)
            .with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()

    @staticmethod
    async def _job(session: AsyncSession, upload_id: str) -> GraphContinuationJob | None:
        return (await session.execute(select(GraphContinuationJob).where(GraphContinuationJob.upload_id == upload_id)
            .with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()

    @staticmethod
    def _ready(record: DocumentUpload | None) -> bool:
        return bool(record and record.status == "ready" and record.stage == "READY" and record.course_uuid
                    and record.content_hash and record.storage_key and record.processed_chunk_count > 0
                    and UploadService.can_reprocess(record))

    @staticmethod
    def _stop(job: GraphContinuationJob, category: str) -> None:
        job.status, job.failure_category = "stopped", category
        job.claim_token = job.lease_owner = job.lease_expires_at = None

    async def schedule(
        self, session: AsyncSession, *, upload_id: str, document_task_id: str,
        chunks: Sequence[DocumentChunk], retry_after_seconds: float,
    ) -> GraphContinuationJob | None:
        record = await self._document(session, upload_id)
        if not self._ready(record) or record.task_id != document_task_id:
            await session.rollback()
            return None
        job = await self.stage_for_ready(session, record=record, chunks=chunks,
                                         retry_after_seconds=retry_after_seconds)
        if job is None:
            await session.rollback()
        else:
            await session.commit()
        return job

    async def stage_for_ready(
        self, session: AsyncSession, *, record: DocumentUpload,
        chunks: Sequence[DocumentChunk], retry_after_seconds: float,
    ) -> GraphContinuationJob | None:
        """Caller owns the document row lock and the commit/rollback boundary.

        Do not reread the document here: populate_existing with autoflush=False
        would discard the caller's uncommitted READY transition. No external I/O.
        """
        if not self._ready(record):
            return None
        if not isinstance(record.result_json, dict) or record.result_json.get("graph_provider_limited") is not True:
            return None  # A free-demo batch budget alone is not a provider reset.
        now = (await session.execute(select(func.clock_timestamp()))).scalar_one()
        deadline = retry_not_before(retry_after_seconds, now)
        if deadline is None:
            raise ValueError("Graph jobs require a valid, bounded provider delay.")
        # The initial READY result already carries the persisted provider reset.
        # A caller's smaller delay must not bypass that existing admission fence.
        remaining = retry_wait_seconds(record.result_json, now)
        if remaining:
            deadline = max(datetime.fromisoformat(deadline), now + timedelta(seconds=remaining)).isoformat()
        payload = create_snapshot(record, chunks)
        digest, contract = snapshot_digest(payload), extraction_contract(chunks)
        existing = await self._job(session, record.upload_id)
        if existing is not None and existing.document_task_id == record.task_id:
            if existing.snapshot_sha256 != digest or existing.contract_sha256 != contract:
                raise ValueError("An existing graph job cannot be reset with different evidence or configuration.")
            return existing  # Duplicate scheduling cannot reset due time/attempts.
        if existing is not None:
            await session.delete(existing)
            await session.flush()
        job = GraphContinuationJob(id=str(uuid4()), upload_id=record.upload_id, document_task_id=record.task_id,
            snapshot_json=payload, snapshot_sha256=digest, contract_sha256=contract,
            status="pending", next_attempt_at=datetime.fromisoformat(deadline), attempt_count=0,
            max_attempts=MAX_GRAPH_PROCESSING_ATTEMPTS - record.attempt_count)
        session.add(job)
        return job

    async def claim_next(self, session: AsyncSession, *, lease_owner: str, lease_seconds: int) -> GraphJobClaim | None:
        if not 1 <= len(lease_owner) <= 128 or type(lease_seconds) is not int or not 1 <= lease_seconds <= 3600:
            raise ValueError("Invalid graph-job lease.")
        # Lock only the joined document first: deletion/manual retry use this same
        # fence. Skip busy documents rather than taking job->document locks.
        upload_id = (await session.execute(select(GraphContinuationJob.upload_id)
            .join(DocumentUpload, DocumentUpload.upload_id == GraphContinuationJob.upload_id)
            .where(or_(
                and_(GraphContinuationJob.status == "pending", GraphContinuationJob.next_attempt_at <= func.clock_timestamp()),
                and_(GraphContinuationJob.status == "running", GraphContinuationJob.lease_expires_at <= func.clock_timestamp()),
            )).order_by(GraphContinuationJob.next_attempt_at, GraphContinuationJob.id).limit(1)
            .with_for_update(of=DocumentUpload, skip_locked=True))).scalar_one_or_none()
        if upload_id is None:
            await session.rollback()
            return None
        record, job = await self._document(session, upload_id), await self._job(session, upload_id)
        now = (await session.execute(select(func.clock_timestamp()))).scalar_one()
        due = job is not None and (
            (job.status == "pending" and job.next_attempt_at <= now)
            or (job.status == "running" and job.lease_expires_at is not None and job.lease_expires_at <= now)
        )
        if not due:
            await session.rollback()
            return None
        if not self._ready(record) or record.task_id != job.document_task_id:
            self._stop(job, "superseded")
        elif record.created_at < now - timedelta(days=settings.demo_upload_retention_days):
            self._stop(job, "retention_expired")
        elif job.attempt_count >= job.max_attempts:
            self._stop(job, "attempt_limit")
        else:
            try:
                chunks = snapshot_chunks(job.snapshot_json, record)
                if snapshot_digest(job.snapshot_json) != job.snapshot_sha256 or extraction_contract(chunks) != job.contract_sha256:
                    raise ValueError("Graph snapshot/configuration changed.")
            except (ValueError, TypeError, KeyError):
                self._stop(job, "incompatible_snapshot")
            else:
                job.status, job.failure_category = "running", None
                job.claim_token, job.lease_owner = str(uuid4()), lease_owner
                job.lease_expires_at = now + timedelta(seconds=lease_seconds)
                job.attempt_count += 1
                claim = GraphJobClaim(job.id, upload_id, job.document_task_id, job.claim_token, lease_owner, chunks)
                await session.commit()
                return claim
        await session.commit()
        return None

    async def _locked_claim(self, session: AsyncSession, claim: GraphJobClaim):
        record, job = await self._document(session, claim.upload_id), await self._job(session, claim.upload_id)
        now = (await session.execute(select(func.clock_timestamp()))).scalar_one()
        if (not self._ready(record) or record.task_id != claim.document_task_id or job is None
            or job.id != claim.job_id or job.document_task_id != claim.document_task_id
            or job.status != "running" or job.claim_token != claim.claim_token or job.lease_owner != claim.lease_owner
            or job.lease_expires_at is None or job.lease_expires_at <= now):
            await session.rollback()
            return None
        return record, job, now

    async def heartbeat(self, session: AsyncSession, claim: GraphJobClaim, *, lease_seconds: int) -> bool:
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 3600:
            raise ValueError("Invalid graph-job lease.")
        locked = await self._locked_claim(session, claim)
        if locked is None:
            return False
        _, job, now = locked
        job.lease_expires_at = now + timedelta(seconds=lease_seconds)
        await session.commit()
        return True

    async def defer(self, session: AsyncSession, claim: GraphJobClaim, *, retry_after_seconds: float) -> bool:
        locked = await self._locked_claim(session, claim)
        if locked is None:
            return False
        _, job, now = locked
        deadline = retry_not_before(retry_after_seconds, now)
        if deadline is None:
            raise ValueError("Graph jobs require a valid, bounded provider delay.")
        job.next_attempt_at = datetime.fromisoformat(deadline)
        if job.attempt_count >= job.max_attempts:
            self._stop(job, "attempt_limit")
        else:
            job.status, job.failure_category = "pending", "provider_quota"
            job.claim_token = job.lease_owner = job.lease_expires_at = None
        await session.commit()
        return True

    async def save_section_checkpoint(
        self, session: AsyncSession, claim: GraphJobClaim, *, batch: GraphSectionBatch,
        extraction: GraphExtractionResponse,
    ) -> bool:
        locked = await self._locked_claim(session, claim)
        if locked is None:
            return False
        record, job, _ = locked
        chunks = snapshot_chunks(job.snapshot_json, record)
        if snapshot_digest(job.snapshot_json) != job.snapshot_sha256 or extraction_contract(chunks) != job.contract_sha256:
            raise ValueError("Graph snapshot/configuration changed.")
        expected = next((candidate for candidate in IngestionService._graph_section_batches(
            chunks, batch_size=settings.graph_batch_size) if candidate.batch_key == batch.batch_key), None)
        if batch.kind != "section" or expected is None:
            raise ValueError("Checkpoint is not a section of this graph job.")
        checked = IngestionService._validated_checkpoint(extraction, expected)
        if checked is None:
            raise ValueError("Graph checkpoint did not pass source/structure validation.")
        checkpoint = (await session.execute(select(GraphExtractionCheckpoint).where(
            GraphExtractionCheckpoint.upload_id == record.upload_id,
            GraphExtractionCheckpoint.batch_key == expected.batch_key))).scalar_one_or_none()
        if checkpoint is None:
            checkpoint = GraphExtractionCheckpoint(id=str(uuid4()), upload_id=record.upload_id,
                batch_key=expected.batch_key, section_key=expected.section_key, section_label=expected.section_label,
                extraction_json=checked.model_dump(mode="json"))
            session.add(checkpoint)
        else:
            checkpoint.extraction_json = checked.model_dump(mode="json")
        await session.commit()
        return True
