"""Durable graph-job admission/claim/checkpoint foundation.

Not connected to the coordinator or any API route yet. Opt-in READY completion
can stage snapshots atomically. This module does not generate or publish a
graph. A future
graph-only worker must publish under the document/job fence before activation.
The disconnected deleted-source reconciliation primitive may also tombstone
an already-deleted upload in Neo4j after a fresh PostgreSQL check.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json
from uuid import uuid4

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.core.exceptions import GraphStructureError
from app.core.processing import MAX_GRAPH_PROCESSING_ATTEMPTS
from app.models.document_upload import DocumentUpload, GraphContinuationJob, GraphExtractionCheckpoint, GraphPublicationCleanupReceipt, GraphPublicationDraft, GraphPublicationIntent, GraphPublicationReceipt
from app.services.graph_retry_policy import retry_not_before, retry_wait_seconds
from app.services.ingestion_service import GraphSectionBatch, IngestionService
from app.services.graph_publication_candidate import (
    GraphPublicationCandidate, prepare_graph_publication, restore_graph_publication,
)
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


def validated_job_checkpoints(chunks: Sequence[DocumentChunk], payloads: dict) -> dict[str, GraphExtractionResponse]:
    """Accept only current section/recovery identities and rebind provenance."""
    decoded = {}
    for key, payload in payloads.items():
        try:
            decoded[key] = IngestionService.decode_graph_checkpoint(payload)
        except (GraphStructureError, ValueError, TypeError, KeyError):
            continue
    completed = {}
    for batch in IngestionService._graph_section_batches(chunks, batch_size=settings.graph_batch_size):
        checked = IngestionService._validated_checkpoint(decoded.get(batch.batch_key), batch)
        if checked is not None:
            completed[batch.batch_key] = checked
    recovery = IngestionService.recovery_checkpoint_batch(chunks, completed)
    if recovery is not None:
        checked = IngestionService._validated_checkpoint(decoded.get(recovery.batch_key), recovery)
        if checked is not None:
            completed[recovery.batch_key] = checked
    return completed


@dataclass(frozen=True, slots=True)
class GraphJobClaim:
    job_id: str
    upload_id: str
    document_task_id: str
    claim_token: str
    lease_owner: str
    chunks: tuple[DocumentChunk, ...]


@dataclass(frozen=True, slots=True)
class GraphJobInput:
    """Detached, validated inputs read together under the active job fence."""

    chunks: tuple[DocumentChunk, ...]
    checkpoints: dict[str, GraphExtractionResponse]


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

    @staticmethod
    async def _snapshot_capacity(session: AsyncSession, upload_id: str, payload: dict) -> bool:
        """Caller holds its document/job fence; admission lock survives to commit.

        Count all retained statuses. Exclude only the replaced upload, never
        delete other jobs to make room. JSONB text bytes are a logical budget,
        not PostgreSQL physical storage, WAL or Python memory usage.
        """
        max_jobs, max_bytes = settings.graph_snapshot_max_jobs, settings.graph_snapshot_max_bytes
        if not max_jobs and not max_bytes:
            return True
        await session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": "conceptgraph:graph-snapshot-admission"})
        # Include earlier staging in this transaction; flush is not a commit.
        await session.flush()
        usage = (await session.execute(text(
            "SELECT count(*) AS jobs, coalesce(sum(octet_length(snapshot_json::jsonb::text)), 0) AS bytes "
            "FROM graph_continuation_jobs WHERE upload_id <> :upload_id "
            "AND snapshot_json::jsonb <> '{}'::jsonb"),
            {"upload_id": upload_id})).one()
        candidate_bytes = (await session.execute(text(
            "SELECT octet_length(CAST(:payload AS jsonb)::text)"),
            {"payload": json.dumps(payload, ensure_ascii=False)})).scalar_one()
        return (not max_jobs or usage.jobs + 1 <= max_jobs) and (
            not max_bytes or usage.bytes + candidate_bytes <= max_bytes)

    async def prune_terminal_snapshots(self, session: AsyncSession, *, older_than_days: int, limit: int = 50) -> int:
        """Drop only old terminal snapshot bodies; caller owns commit/rollback.

        Preserve job identity/digests/attempts as a tombstone against automatic
        re-admission. This internal method is not scheduled in production.
        """
        if type(older_than_days) is not int or not 1 <= older_than_days <= 365:
            raise ValueError("Invalid snapshot retention period.")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Invalid snapshot cleanup batch size.")
        now = (await session.execute(select(func.clock_timestamp()))).scalar_one()
        cutoff = now - timedelta(days=older_than_days)
        candidates = (await session.execute(select(DocumentUpload.upload_id).join(
            GraphContinuationJob, GraphContinuationJob.upload_id == DocumentUpload.upload_id).where(
                GraphContinuationJob.status.in_(("stopped", "completed")),
                GraphContinuationJob.updated_at < cutoff,
                text("graph_continuation_jobs.snapshot_json::jsonb <> '{}'::jsonb"))
            .order_by(GraphContinuationJob.updated_at, DocumentUpload.upload_id).limit(limit)
            .with_for_update(of=DocumentUpload, skip_locked=True))).scalars().all()
        pruned = 0
        for upload_id in candidates:
            job = await self._job(session, upload_id)
            if job is not None and job.status in {"stopped", "completed"} and job.updated_at < cutoff and job.snapshot_json:
                job.snapshot_json = {}
                pruned += 1
        return pruned

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
        if not await self._snapshot_capacity(session, record.upload_id, payload):
            return None
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
        return await self.save_checkpoint(session, claim, batch=batch, extraction=extraction, allow_global=False)

    async def load_checkpoints(
        self, session: AsyncSession, claim: GraphJobClaim,
    ) -> dict[str, GraphExtractionResponse] | None:
        inputs = await self.load_input(session, claim)
        return None if inputs is None else inputs.checkpoints

    async def prepare_publication(
        self, session: AsyncSession, claim: GraphJobClaim, extraction: GraphExtractionResponse,
    ) -> GraphPublicationCandidate | None:
        """Preflight under current document/job locks; caller owns transaction.

        Unlike load_input, retain locks until caller commits/rolls back. The
        returned value is not durable authority, a receipt or permission to
        publish after releasing this transaction. No remote writes occur here.
        """
        sources = await self._publication_sources(session, claim)
        if sources is None:
            return None
        record, chunks = sources
        return prepare_graph_publication(extraction, chunks, upload_id=record.upload_id,
            course_id=record.course_uuid, document_task_id=record.task_id,
            document_name=record.original_filename)

    async def restore_publication(
        self, session: AsyncSession, claim: GraphJobClaim, candidate: GraphPublicationCandidate,
    ) -> GraphExtractionResponse | None:
        """Validate replay under current locks, without releasing or completing them.

        No saved identity or caller-supplied chunks establish authority. Caller
        owns commit/rollback, including on validation errors; no remote I/O occurs.
        This is not a durable receipt or cross-store reconciliation operation.
        """
        sources = await self._publication_sources(session, claim)
        if sources is None:
            return None
        record, chunks = sources
        return restore_graph_publication(candidate, chunks, upload_id=record.upload_id,
            course_id=record.course_uuid, document_task_id=record.task_id,
            document_name=record.original_filename)

    async def _publication_sources(self, session: AsyncSession, claim: GraphJobClaim):
        locked = await self._locked_claim(session, claim)
        if locked is None:
            return None
        record, job, _ = locked
        chunks = snapshot_chunks(job.snapshot_json, record)
        if snapshot_digest(job.snapshot_json) != job.snapshot_sha256 or extraction_contract(chunks) != job.contract_sha256:
            raise ValueError("Graph snapshot/configuration changed.")
        return record, chunks

    async def save_publication_draft(
        self, session: AsyncSession, claim: GraphJobClaim, candidate: GraphPublicationCandidate,
    ) -> bool:
        """Stage one immutable draft under the claim; caller owns commit/rollback.

        This is not a dispatch/commit intent or receipt. It is safe to discard
        with the job because no external write is initiated by this operation.
        """
        if await self.restore_publication(session, claim, candidate) is None:
            return False
        if len(candidate.payload_json.encode("utf-8")) > MAX_SNAPSHOT_BYTES:
            raise ValueError("Graph publication draft exceeds its storage bound.")
        # Same-transaction replay must see a prior staged insert even with the
        # application's autoflush=False session configuration.
        await session.flush()
        saved = (await session.execute(select(GraphPublicationDraft).where(
            GraphPublicationDraft.job_id == claim.job_id).with_for_update()
            .execution_options(populate_existing=True))).scalar_one_or_none()
        if saved is not None:
            if (saved.payload_json, saved.payload_sha256) != (candidate.payload_json, candidate.sha256):
                raise ValueError("Graph job already has a different publication draft.")
            return True
        if not await self._draft_capacity(session, candidate):
            return False
        session.add(GraphPublicationDraft(job_id=claim.job_id,
            payload_json=candidate.payload_json, payload_sha256=candidate.sha256))
        return True

    @staticmethod
    async def _draft_capacity(session: AsyncSession, candidate: GraphPublicationCandidate) -> bool:
        """Serialize new admissions; count every retained draft, not only active jobs."""
        max_jobs, max_bytes = settings.graph_draft_max_jobs, settings.graph_draft_max_bytes
        if not max_jobs and not max_bytes:
            return True
        await session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": "conceptgraph:graph-snapshot-admission"})
        await session.flush()
        usage = (await session.execute(select(func.count().label("jobs"),
            func.coalesce(func.sum(func.octet_length(GraphPublicationDraft.payload_json)), 0).label("bytes")))).one()
        return (not max_jobs or usage.jobs + 1 <= max_jobs) and (
            not max_bytes or usage.bytes + len(candidate.payload_json.encode("utf-8")) <= max_bytes)

    async def prune_terminal_drafts(self, session: AsyncSession, *, older_than_days: int, limit: int = 50) -> int:
        """Remove old unpublished drafts only; caller owns transaction, no scheduler.

        Preserve job tombstones/attempts, snapshots, READY results and checkpoints.
        These cascading drafts must never serve as remote-write receipts.
        """
        if type(older_than_days) is not int or not 1 <= older_than_days <= 365:
            raise ValueError("Invalid draft retention period.")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Invalid draft cleanup batch size.")
        await session.flush()  # Repeated caller-owned cleanup must see pending deletes.
        cutoff = (await session.execute(select(func.clock_timestamp()))).scalar_one() - timedelta(days=older_than_days)
        uploads = (await session.execute(select(DocumentUpload.upload_id).join(
            GraphContinuationJob, GraphContinuationJob.upload_id == DocumentUpload.upload_id).join(
                GraphPublicationDraft, GraphPublicationDraft.job_id == GraphContinuationJob.id).where(
                    GraphContinuationJob.status.in_(("stopped", "completed")), GraphContinuationJob.updated_at < cutoff)
            .order_by(GraphContinuationJob.updated_at, DocumentUpload.upload_id).limit(limit)
            .with_for_update(of=DocumentUpload, skip_locked=True))).scalars().all()
        pruned = 0
        for upload_id in uploads:
            job = await self._job(session, upload_id)
            if job is None or job.status not in {"stopped", "completed"} or job.updated_at >= cutoff:
                continue
            draft = (await session.execute(select(GraphPublicationDraft).where(
                GraphPublicationDraft.job_id == job.id).with_for_update()
                .execution_options(populate_existing=True))).scalar_one_or_none()
            if draft is not None:
                await session.delete(draft)
                pruned += 1
        return pruned

    async def load_publication_draft(
        self, session: AsyncSession, claim: GraphJobClaim,
    ) -> GraphPublicationCandidate | None:
        """Read/revalidate a saved draft under current locks; caller owns transaction.

        None means missing draft or lost authority, never permission to publish.
        Locks must not be released before a future publisher rechecks authority.
        """
        sources = await self._publication_sources(session, claim)
        if sources is None:
            return None
        await session.flush()
        saved = (await session.execute(select(GraphPublicationDraft).where(
            GraphPublicationDraft.job_id == claim.job_id).with_for_update()
            .execution_options(populate_existing=True))).scalar_one_or_none()
        if saved is None:
            return None
        candidate = GraphPublicationCandidate(saved.payload_json, saved.payload_sha256)
        record, chunks = sources
        restore_graph_publication(candidate, chunks, upload_id=record.upload_id,
            course_id=record.course_uuid, document_task_id=record.task_id,
            document_name=record.original_filename)
        return candidate

    async def stage_publication_intent(
        self, session: AsyncSession, claim: GraphJobClaim,
    ) -> GraphPublicationIntent | None:
        """Record an immutable remote-write possibility; caller commits before I/O.

        This disconnected primitive neither authorizes a later write nor records
        its outcome. A future publisher must refence the document/job after
        committing this row, and reconcile ambiguous writes and deletions.
        """
        candidate = await self.load_publication_draft(session, claim)
        if candidate is None:
            return None
        locked = await self._locked_claim(session, claim)
        if locked is None:
            return None
        record, job, _ = locked
        generation = record.attempt_count
        if type(generation) is not int or generation <= 0:
            raise ValueError("Graph publication requires a positive source generation.")
        identity = (job.id, record.upload_id, record.task_id, record.course_uuid,
                    candidate.sha256, generation)
        await session.flush()  # See a staged intent with autoflush disabled.
        saved = (await session.execute(select(GraphPublicationIntent).where(
            GraphPublicationIntent.job_id == job.id).with_for_update()
            .execution_options(populate_existing=True))).scalar_one_or_none()
        if saved is not None:
            if (saved.job_id, saved.upload_id, saved.document_task_id,
                    saved.course_id, saved.payload_sha256, saved.source_generation) != identity:
                raise ValueError("Graph job already has a different publication intent.")
            return saved
        intent = GraphPublicationIntent(id=str(uuid4()), job_id=job.id,
            upload_id=record.upload_id, document_task_id=record.task_id,
            course_id=record.course_uuid, payload_sha256=candidate.sha256,
            source_generation=generation)
        session.add(intent)
        return intent

    @staticmethod
    async def list_deleted_source_intents(
        session: AsyncSession, *, limit: int = 50,
    ) -> list[GraphPublicationIntent]:
        """Find bounded intent tombstones after upload deletion, without mutating them.

        This is discovery only: an absent PostgreSQL upload is not proof that a
        remote write has finished. A future reconciler must inspect the exact
        Neo4j marker and repeat after late writers have drained.
        """
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Invalid deleted-source discovery batch size.")
        result = await session.execute(select(GraphPublicationIntent).outerjoin(
            DocumentUpload, DocumentUpload.upload_id == GraphPublicationIntent.upload_id).outerjoin(
            GraphPublicationCleanupReceipt,
            GraphPublicationCleanupReceipt.intent_id == GraphPublicationIntent.id).where(
            DocumentUpload.upload_id.is_(None),
            GraphPublicationCleanupReceipt.intent_id.is_(None)).order_by(
            GraphPublicationIntent.created_at, GraphPublicationIntent.id).limit(limit))
        return result.scalars().all()

    @staticmethod
    async def reconcile_deleted_source_publication(
        sessions: async_sessionmaker[AsyncSession], intent_id: str,
        ingestion_service: IngestionService,
    ) -> bool:
        """Tombstone one retained intent only after a fresh committed-source check.

        Internal manual recovery primitive with no worker or scheduler caller.
        A new PostgreSQL session cannot observe an uncommitted deletion from
        the caller; FOR UPDATE waits for a concurrent delete to finish.
        The Neo4j tombstone is idempotent if PostgreSQL commit becomes unclear.
        """
        if not isinstance(intent_id, str) or not intent_id.strip():
            raise ValueError("Graph publication intent ID is required.")
        async with sessions() as session:
            async with session.begin():
                intent = (await session.execute(select(GraphPublicationIntent).where(
                    GraphPublicationIntent.id == intent_id))).scalar_one_or_none()
                if intent is None:
                    return False
                source = (await session.execute(select(DocumentUpload.upload_id).where(
                    DocumentUpload.upload_id == intent.upload_id).with_for_update()
                )).scalar_one_or_none()
                if source is not None:
                    return False
                # Only one reconciler may call the remote tombstone for this
                # intent. The transaction lock lasts through receipt commit.
                await session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                    {"key": f"conceptgraph:graph-publication-cleanup:{intent.id}"})
                receipt = (await session.execute(select(GraphPublicationCleanupReceipt).where(
                    GraphPublicationCleanupReceipt.intent_id == intent.id))).scalar_one_or_none()
                if receipt is not None:
                    if (receipt.upload_id, receipt.course_id) != (intent.upload_id, intent.course_id):
                        raise ValueError("Graph publication cleanup receipt conflicts with intent.")
                    return False
                await ingestion_service.tombstone_graph_publication(
                    intent.upload_id, intent.course_id)
                session.add(GraphPublicationCleanupReceipt(intent_id=intent.id,
                    upload_id=intent.upload_id, course_id=intent.course_id))
                return True

    async def observe_publication_receipt(
        self, session: AsyncSession, claim: GraphJobClaim, ingestion_service: IngestionService,
    ) -> GraphPublicationReceipt | None:
        """Record an exact remote marker under the current source/job fence.

        This is a disconnected observation, not graph-job completion. The caller
        owns the PostgreSQL commit/rollback boundary and must release locks on
        all paths. A future publisher still needs crash/deletion reconciliation.
        """
        candidate = await self.load_publication_draft(session, claim)
        if candidate is None:
            return None
        locked = await self._locked_claim(session, claim)
        if locked is None:
            return None
        record, job, _ = locked
        intent = (await session.execute(select(GraphPublicationIntent).where(
            GraphPublicationIntent.job_id == job.id).with_for_update()
            .execution_options(populate_existing=True))).scalar_one_or_none()
        if intent is None:
            return None
        generation = record.attempt_count
        if type(generation) is not int or generation <= 0:
            raise ValueError("Graph publication requires a positive source generation.")
        identity = (intent.id, job.id, record.upload_id, record.course_uuid,
                    record.task_id, candidate.sha256, generation)
        if (intent.job_id, intent.upload_id, intent.course_id,
                intent.document_task_id, intent.payload_sha256,
                intent.source_generation) != identity[1:]:
            raise ValueError("Graph publication intent no longer matches the source.")
        marker = await ingestion_service.read_graph_publication_marker(intent.id)
        if marker is None:
            return None
        if (marker.id, marker.job_id, marker.upload_id, marker.course_id,
                marker.document_task_id, marker.payload_sha256,
                marker.generation) != identity:
            raise ValueError("Remote graph marker conflicts with the publication intent.")
        refenced = await self._locked_claim(session, claim)
        if refenced is None or refenced[0].attempt_count != generation:
            return None  # The lease may have expired during remote I/O.
        await session.flush()
        receipt = (await session.execute(select(GraphPublicationReceipt).where(
            GraphPublicationReceipt.intent_id == intent.id).with_for_update()
            .execution_options(populate_existing=True))).scalar_one_or_none()
        if receipt is not None:
            if (receipt.intent_id, receipt.job_id, receipt.upload_id, receipt.course_id,
                    receipt.document_task_id, receipt.payload_sha256,
                    receipt.source_generation) != identity:
                raise ValueError("Graph publication receipt conflicts with the observed marker.")
            return receipt
        receipt = GraphPublicationReceipt(intent_id=intent.id, job_id=job.id,
            upload_id=record.upload_id, course_id=record.course_uuid,
            document_task_id=record.task_id, payload_sha256=candidate.sha256,
            source_generation=generation)
        session.add(receipt)
        return receipt

    async def load_input(
        self, session: AsyncSession, claim: GraphJobClaim,
    ) -> GraphJobInput | None:
        """Read under the current claim, then release locks before extraction.

        None means lost authority, not an empty cache. Ignore claim.chunks: the
        persisted source/contract is authoritative. No external I/O or mutation.
        """
        try:
            locked = await self._locked_claim(session, claim)
            if locked is None:
                return None
            record, job, _ = locked
            chunks = snapshot_chunks(job.snapshot_json, record)
            if snapshot_digest(job.snapshot_json) != job.snapshot_sha256 or extraction_contract(chunks) != job.contract_sha256:
                raise ValueError("Graph snapshot/configuration changed.")
            rows = (await session.execute(select(GraphExtractionCheckpoint).where(
                GraphExtractionCheckpoint.upload_id == record.upload_id)
                .execution_options(populate_existing=True))).scalars().all()
            return GraphJobInput(tuple(chunks), validated_job_checkpoints(
                chunks, {row.batch_key: row.extraction_json for row in rows}))
        finally:
            await session.rollback()  # Read-only boundary; release document/job locks.

    async def save_checkpoint(
        self, session: AsyncSession, claim: GraphJobClaim, *, batch: GraphSectionBatch,
        extraction: GraphExtractionResponse, allow_global: bool = True,
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
        if batch.kind == "global" and allow_global:
            rows = (await session.execute(select(GraphExtractionCheckpoint).where(
                GraphExtractionCheckpoint.upload_id == record.upload_id))).scalars().all()
            completed = {}
            for row in rows:
                try:
                    completed[row.batch_key] = IngestionService.decode_graph_checkpoint(row.extraction_json)
                except (GraphStructureError, ValueError, TypeError, KeyError):
                    continue
            expected = IngestionService.recovery_checkpoint_batch(chunks, completed)
        if expected is None or expected.kind != batch.kind or expected.batch_key != batch.batch_key:
            raise ValueError("Checkpoint does not match this graph job's current source and concepts.")
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
