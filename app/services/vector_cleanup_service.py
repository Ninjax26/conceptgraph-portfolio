"""Repeatable, source-fenced cleanup of writes that outlive upload deletion."""
from dataclasses import dataclass
from datetime import timedelta
import hashlib
import json
from urllib.parse import urlsplit

from sqlalchemy import exists, func, literal, select, union
from sqlalchemy.dialects.postgresql import insert

from app.core.blocking import run_async_safely, run_blocking_safely
from app.models.document_upload import DocumentUpload, ProcessingAttempt, VectorDeletionTombstone
from app.services.resource_budget_service import resource_budget_service


@dataclass(frozen=True)
class VectorCleanupScope:
    collection_name: str
    store_sha256: str

    @classmethod
    def from_url(cls, url: str, collection_name: str):
        parsed = urlsplit(url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.query or parsed.fragment
            or not isinstance(collection_name, str) or not collection_name.strip() or len(collection_name) > 255):
            raise ValueError("Invalid vector cleanup store binding.")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        identity = ["vector-cleanup-v1", parsed.scheme, parsed.hostname, port,
                    parsed.path.rstrip("/"), collection_name]
        return cls(collection_name, hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest())


class VectorCleanupService:
    RECHECK_SECONDS = 300

    @staticmethod
    async def stage(session, record, scope: VectorCleanupScope):
        if not all(isinstance(value, str) and value.strip() for value in (record.upload_id, record.task_id)):
            raise ValueError("Vector deletion tracking requires a source execution identity.")
        # SQL UNION avoids loading attempt history or duplicating the current
        # task. Legacy current tasks without an attempt row are still retained.
        tasks = union(select(ProcessingAttempt.task_id.label("task_id")).where(
            ProcessingAttempt.document_id == record.upload_id),
            select(literal(record.task_id).label("task_id"))).subquery()
        statement = insert(VectorDeletionTombstone).from_select(
            ["upload_id", "task_id", "store_sha256", "collection_name"],
            select(literal(record.upload_id), tasks.c.task_id,
                   literal(scope.store_sha256), literal(scope.collection_name)))
        await session.execute(statement.on_conflict_do_nothing(
            index_elements=["upload_id", "task_id", "store_sha256"]))

    @staticmethod
    async def due(session, scope: VectorCleanupScope, *, limit=50):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Invalid vector cleanup batch size.")
        row = VectorDeletionTombstone
        result = await session.execute(select(row.upload_id, row.task_id).where(
            row.store_sha256 == scope.store_sha256, row.collection_name == scope.collection_name,
            row.next_attempt_at <= func.now(),
            ~exists(select(DocumentUpload.upload_id).where(DocumentUpload.upload_id == row.upload_id)),
        ).order_by(row.next_attempt_at, row.upload_id, row.task_id).limit(limit))
        return list(result.all())

    @classmethod
    async def reconcile(cls, sessions, upload_id, task_id, scope: VectorCleanupScope, ingestion):
        if ingestion.collection_name != scope.collection_name:
            return False

        async def fenced():
            async with sessions() as session:
                # Match the admission lock/order used by upload/retry/deletion.
                # Background work yields immediately if foreground work holds it.
                if not await resource_budget_service.try_lock(session):
                    return False
                row = (await session.execute(select(VectorDeletionTombstone).where(
                    VectorDeletionTombstone.upload_id == upload_id,
                    VectorDeletionTombstone.task_id == task_id,
                    VectorDeletionTombstone.store_sha256 == scope.store_sha256,
                    VectorDeletionTombstone.next_attempt_at <= func.now(),
                ).with_for_update(skip_locked=True))).scalar_one_or_none()
                if row is None or row.collection_name != scope.collection_name or row.store_sha256 != scope.store_sha256:
                    return False
                present = (await session.execute(select(exists().where(
                    DocumentUpload.upload_id == upload_id)))).scalar_one()
                checked = False
                row.next_attempt_at = func.clock_timestamp() + timedelta(seconds=cls.RECHECK_SECONDS)
                if not present:
                    row.last_checked_at = func.clock_timestamp()
                    try:
                        await run_blocking_safely(ingestion.cleanup_vectors, upload_id, execution_token=task_id)
                    except Exception as error:
                        # Retain authority and retry after ambiguous acknowledgements.
                        # Persist only the type, never provider text/URLs/documents.
                        row.last_error_type = type(error).__name__[:64]
                    else:
                        row.last_error_type = None
                        checked = True
                await session.commit()
                return checked
        return await run_async_safely(fenced())
