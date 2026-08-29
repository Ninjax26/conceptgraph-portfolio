from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.processing import FailureCategory, MAX_PROCESSING_ATTEMPTS, ProcessingStage
from app.models.document_upload import Course, DocumentUpload, ProcessingAttempt


class UploadService:
    DELETABLE_STAGES = {
        ProcessingStage.READY.value,
        ProcessingStage.FAILED.value,
    }

    async def create_upload(
        self,
        session: AsyncSession,
        *,
        upload_id: str,
        task_id: str,
        course: Course,
        content_hash: str,
        original_filename: str,
        storage_key: str,
    ) -> DocumentUpload:
        now = datetime.now(timezone.utc)
        record = DocumentUpload(
            upload_id=upload_id,
            task_id=task_id,
            course_id=course.display_name,
            course_uuid=course.id,
            content_hash=content_hash,
            # Kept internally until the database schema is migrated; it is no longer
            # part of the product or retrieval model.
            week_number=1,
            original_filename=original_filename,
            storage_key=storage_key,
            status="active",
            stage=ProcessingStage.UPLOADED.value,
            retryable=False,
            attempt_count=1,
            last_attempted_at=now,
            last_heartbeat_at=now,
        )
        session.add(record)
        session.add(
            ProcessingAttempt(
                id=str(uuid4()), document_id=upload_id, task_id=task_id,
                attempt_number=1, stage=ProcessingStage.UPLOADED.value,
                last_heartbeat_at=now,
            )
        )
        await session.commit()
        await session.refresh(record)
        return record

    async def find_duplicate(
        self, session: AsyncSession, course_uuid: str, content_hash: str
    ) -> DocumentUpload | None:
        result = await session.execute(
            select(DocumentUpload)
            .where(
                DocumentUpload.course_uuid == course_uuid,
                DocumentUpload.content_hash == content_hash,
            )
            .order_by(desc(DocumentUpload.created_at))
        )
        return result.scalars().first()

    async def count_uploads(self, session: AsyncSession) -> int:
        result = await session.execute(select(func.count(DocumentUpload.upload_id)))
        return int(result.scalar_one())

    async def get_upload(self, session: AsyncSession, upload_id: str) -> DocumentUpload | None:
        result = await session.execute(
            select(DocumentUpload).where(DocumentUpload.upload_id == upload_id)
        )
        return result.scalar_one_or_none()

    async def get_upload_by_task_id(
        self,
        session: AsyncSession,
        task_id: str,
    ) -> DocumentUpload | None:
        result = await session.execute(
            select(DocumentUpload).where(DocumentUpload.task_id == task_id)
        )
        record = result.scalar_one_or_none()
        if record is not None:
            return record
        historical = await session.execute(
            select(DocumentUpload)
            .join(
                ProcessingAttempt,
                ProcessingAttempt.document_id == DocumentUpload.upload_id,
            )
            .where(ProcessingAttempt.task_id == task_id)
        )
        return historical.scalar_one_or_none()

    async def list_uploads(
        self,
        session: AsyncSession,
        limit: int = 25,
    ) -> list[DocumentUpload]:
        result = await session.execute(
            select(DocumentUpload).order_by(desc(DocumentUpload.created_at)).limit(limit)
        )
        return list(result.scalars().all())

    async def list_dispatchable(
        self,
        session: AsyncSession,
        *,
        limit: int,
    ) -> list[DocumentUpload]:
        now = datetime.now(timezone.utc)
        result = await session.execute(
            select(DocumentUpload)
            .where(
                DocumentUpload.status == "active",
                DocumentUpload.stage == ProcessingStage.UPLOADED.value,
                or_(
                    DocumentUpload.lease_expires_at.is_(None),
                    DocumentUpload.lease_expires_at <= now,
                ),
            )
            .order_by(DocumentUpload.last_attempted_at, DocumentUpload.created_at)
            .limit(limit)
        )
        return list(result.scalars().all())

    async def prepare_stale_recoveries(
        self,
        session: AsyncSession,
    ) -> list[DocumentUpload]:
        """Turn expired interrupted executions into real, fenced attempts.

        Fresh UPLOADED rows have not started yet and keep their original attempt.
        Any later active stage without a valid lease represents interrupted work.
        """

        now = datetime.now(timezone.utc)
        result = await session.execute(
            select(DocumentUpload)
            .where(
                DocumentUpload.status == "active",
                DocumentUpload.stage != ProcessingStage.UPLOADED.value,
                or_(
                    DocumentUpload.lease_expires_at.is_(None),
                    DocumentUpload.lease_expires_at <= now,
                ),
            )
            .with_for_update(skip_locked=True)
        )
        recovered: list[DocumentUpload] = []
        for record in result.scalars().all():
            previous_attempt = await self._current_attempt(session, record)
            retry_available = record.attempt_count < MAX_PROCESSING_ATTEMPTS
            if previous_attempt is not None:
                previous_attempt.stage = ProcessingStage.FAILED.value
                previous_attempt.failure_category = FailureCategory.WORKER_ERROR.value
                previous_attempt.retryable = retry_available
                previous_attempt.error_message = "Processing was interrupted by an application restart."
                previous_attempt.completed_at = now
                previous_attempt.last_heartbeat_at = now

            if not retry_available:
                record.status = "failed"
                record.stage = ProcessingStage.FAILED.value
                record.failure_category = FailureCategory.WORKER_ERROR.value
                record.retryable = False
                record.error_message = (
                    "The retry limit was reached after repeated processing interruptions. "
                    "Remove this failed record and upload the PDF again."
                )
                record.completed_at = now
                record.last_heartbeat_at = now
                record.lease_owner = None
                record.lease_expires_at = None
                continue

            new_task_id = str(uuid4())
            record.task_id = new_task_id
            record.status = "active"
            record.stage = ProcessingStage.UPLOADED.value
            record.failure_category = None
            record.retryable = False
            record.error_message = None
            record.result_json = None
            record.attempt_count += 1
            record.last_attempted_at = now
            record.last_heartbeat_at = now
            record.started_at = None
            record.completed_at = None
            record.lease_owner = None
            record.lease_expires_at = None
            session.add(
                ProcessingAttempt(
                    id=str(uuid4()),
                    document_id=record.upload_id,
                    task_id=new_task_id,
                    attempt_number=record.attempt_count,
                    stage=ProcessingStage.UPLOADED.value,
                    last_heartbeat_at=now,
                )
            )
            recovered.append(record)

        await session.commit()
        return recovered

    async def claim_for_processing(
        self,
        session: AsyncSession,
        *,
        upload_id: str,
        task_id: str,
        lease_owner: str,
        lease_seconds: int,
    ) -> DocumentUpload | None:
        result = await session.execute(
            select(DocumentUpload)
            .where(
                DocumentUpload.upload_id == upload_id,
                DocumentUpload.task_id == task_id,
            )
            .with_for_update(skip_locked=True)
        )
        record = result.scalar_one_or_none()
        now = datetime.now(timezone.utc)
        if (
            record is None
            or record.status != "active"
            or record.stage != ProcessingStage.UPLOADED.value
            or (
                record.lease_expires_at is not None
                and record.lease_expires_at > now
                and record.lease_owner != lease_owner
            )
        ):
            await session.rollback()
            return None

        record.lease_owner = lease_owner
        record.lease_expires_at = now + timedelta(seconds=lease_seconds)
        record.last_heartbeat_at = now
        attempt = await self._current_attempt(session, record)
        if attempt is not None:
            attempt.last_heartbeat_at = now
        await session.commit()
        await session.refresh(record)
        return record

    async def retry_upload(
        self,
        session: AsyncSession,
        upload_id: str,
        task_id: str,
    ) -> DocumentUpload | None:
        result = await session.execute(
            select(DocumentUpload)
            .where(DocumentUpload.upload_id == upload_id)
            .with_for_update()
        )
        record = result.scalar_one_or_none()
        if (
            record is None
            or record.stage != ProcessingStage.FAILED.value
            or not record.retryable
            or record.attempt_count >= MAX_PROCESSING_ATTEMPTS
        ):
            return None
        record.task_id = task_id
        record.status = "active"
        record.stage = ProcessingStage.UPLOADED.value
        record.error_message = None
        record.result_json = None
        record.failure_category = None
        record.retryable = False
        record.attempt_count += 1
        record.last_attempted_at = datetime.now(timezone.utc)
        record.last_heartbeat_at = record.last_attempted_at
        record.lease_owner = None
        record.lease_expires_at = None
        record.started_at = None
        record.completed_at = None
        session.add(
            ProcessingAttempt(
                id=str(uuid4()), document_id=record.upload_id, task_id=task_id,
                attempt_number=record.attempt_count, stage=ProcessingStage.UPLOADED.value,
                last_heartbeat_at=record.last_heartbeat_at,
            )
        )
        await session.commit()
        await session.refresh(record)
        return record

    async def delete_document(
        self,
        session: AsyncSession,
        upload_id: str,
    ) -> DocumentUpload | None:
        record = await self.get_upload(session, upload_id)
        if record is None or record.stage not in self.DELETABLE_STAGES:
            return None
        course_uuid = record.course_uuid
        await session.delete(record)
        await session.flush()
        if course_uuid:
            remaining = await session.execute(
                select(func.count(DocumentUpload.upload_id)).where(
                    DocumentUpload.course_uuid == course_uuid
                )
            )
            if int(remaining.scalar_one()) == 0:
                course_result = await session.execute(
                    select(Course).where(Course.id == course_uuid)
                )
                course = course_result.scalar_one_or_none()
                if course is not None:
                    await session.delete(course)
        await session.commit()
        return record

    async def lock_for_deletion(
        self,
        session: AsyncSession,
        upload_id: str,
    ) -> DocumentUpload | None:
        result = await session.execute(
            select(DocumentUpload)
            .where(DocumentUpload.upload_id == upload_id)
            .with_for_update()
        )
        record = result.scalar_one_or_none()
        if record is None or record.stage not in self.DELETABLE_STAGES:
            return None
        return record

    async def storage_key_is_shared(
        self,
        session: AsyncSession,
        storage_key: str,
        excluding_upload_id: str,
    ) -> bool:
        result = await session.execute(
            select(func.count(DocumentUpload.upload_id)).where(
                DocumentUpload.storage_key == storage_key,
                DocumentUpload.upload_id != excluding_upload_id,
            )
        )
        return int(result.scalar_one()) > 0

    async def set_stage(
        self,
        session: AsyncSession,
        upload_id: str,
        task_id: str,
        stage: ProcessingStage,
        *,
        lease_owner: str | None = None,
        lease_seconds: int | None = None,
    ) -> bool:
        record = await self._get_current_upload(
            session, upload_id, task_id, lease_owner=lease_owner
        )
        if record is None or record.status != "active":
            return False
        now = datetime.now(timezone.utc)
        record.stage = stage.value
        record.status = "active" if stage != ProcessingStage.READY else "ready"
        record.last_heartbeat_at = now
        if lease_owner is not None and lease_seconds is not None:
            record.lease_expires_at = now + timedelta(seconds=lease_seconds)
        if record.started_at is None:
            record.started_at = now
        attempt = await self._current_attempt(session, record)
        if attempt is not None:
            attempt.stage = stage.value
            if attempt.started_at is None:
                attempt.started_at = record.started_at
            attempt.last_heartbeat_at = now
        await session.commit()
        return True

    async def heartbeat(
        self,
        session: AsyncSession,
        upload_id: str,
        task_id: str,
        *,
        lease_owner: str | None = None,
        lease_seconds: int | None = None,
    ) -> bool:
        record = await self._get_current_upload(
            session, upload_id, task_id, lease_owner=lease_owner
        )
        if record is None or record.status != "active":
            return False
        now = datetime.now(timezone.utc)
        record.last_heartbeat_at = now
        if lease_owner is not None and lease_seconds is not None:
            record.lease_expires_at = now + timedelta(seconds=lease_seconds)
        attempt = await self._current_attempt(session, record)
        if attempt is not None:
            attempt.last_heartbeat_at = now
        await session.commit()
        return True

    async def mark_completed(
        self,
        session: AsyncSession,
        upload_id: str,
        task_id: str,
        result_json: dict[str, Any],
        *,
        lease_owner: str | None = None,
    ) -> bool:
        record = await self._get_current_upload(
            session, upload_id, task_id, lease_owner=lease_owner
        )
        if record is None or record.status != "active":
            return False
        chunk_count = int(result_json.get("chunks_indexed", 0))
        if (
            record.stage != ProcessingStage.GRAPH_BUILT.value
            or not record.course_uuid
            or not record.storage_key
            or chunk_count <= 0
        ):
            raise ValueError(
                "READY requires GRAPH_BUILT, a canonical course, a source object key, "
                "and at least one committed chunk."
            )
        record.status = "ready"
        record.stage = ProcessingStage.READY.value
        record.result_json = result_json
        record.processed_chunk_count = chunk_count
        record.graph_node_count = int(result_json.get("nodes_upserted", 0))
        record.graph_edge_count = int(result_json.get("relationships_upserted", 0))
        record.completed_at = datetime.now(timezone.utc)
        record.error_message = None
        record.failure_category = None
        record.retryable = False
        record.last_heartbeat_at = record.completed_at
        record.lease_owner = None
        record.lease_expires_at = None
        attempt = await self._current_attempt(session, record)
        if attempt is not None:
            attempt.stage = ProcessingStage.READY.value
            attempt.completed_at = record.completed_at
            attempt.last_heartbeat_at = record.completed_at
        await session.commit()
        return True

    async def mark_failed(
        self,
        session: AsyncSession,
        upload_id: str,
        task_id: str,
        error_message: str,
        category: FailureCategory = FailureCategory.UNKNOWN_ERROR,
        retryable: bool = False,
        *,
        lease_owner: str | None = None,
    ) -> bool:
        record = await self._get_current_upload(
            session, upload_id, task_id, lease_owner=lease_owner
        )
        if record is None or record.status != "active":
            return False
        record.status = "failed"
        record.stage = ProcessingStage.FAILED.value
        retry_limit_reached = (
            retryable and record.attempt_count >= MAX_PROCESSING_ATTEMPTS
        )
        safe_message = (
            "The retry limit was reached after repeated temporary failures. "
            "Remove this failed record and upload the PDF again."
            if retry_limit_reached
            else error_message
        )
        record.error_message = safe_message
        record.failure_category = category.value
        record.retryable = retryable and record.attempt_count < MAX_PROCESSING_ATTEMPTS
        record.completed_at = datetime.now(timezone.utc)
        record.last_heartbeat_at = record.completed_at
        record.lease_owner = None
        record.lease_expires_at = None
        attempt = await self._current_attempt(session, record)
        if attempt is not None:
            attempt.stage = ProcessingStage.FAILED.value
            attempt.failure_category = category.value
            attempt.retryable = record.retryable
            attempt.error_message = safe_message
            attempt.completed_at = record.completed_at
            attempt.last_heartbeat_at = record.completed_at
        await session.commit()
        return True

    async def release_lease(
        self,
        session: AsyncSession,
        upload_id: str,
        task_id: str,
        lease_owner: str,
    ) -> bool:
        record = await self._get_current_upload(
            session, upload_id, task_id, lease_owner=lease_owner
        )
        if record is None or record.status != "active":
            return False
        record.lease_owner = None
        record.lease_expires_at = None
        record.last_heartbeat_at = datetime.now(timezone.utc)
        await session.commit()
        return True

    async def _get_current_upload(
        self,
        session: AsyncSession,
        upload_id: str,
        task_id: str,
        *,
        lease_owner: str | None = None,
    ) -> DocumentUpload | None:
        conditions = [
            DocumentUpload.upload_id == upload_id,
            DocumentUpload.task_id == task_id,
        ]
        if lease_owner is not None:
            conditions.append(DocumentUpload.lease_owner == lease_owner)
        result = await session.execute(select(DocumentUpload).where(*conditions))
        return result.scalar_one_or_none()

    async def _current_attempt(
        self, session: AsyncSession, record: DocumentUpload
    ) -> ProcessingAttempt | None:
        result = await session.execute(
            select(ProcessingAttempt).where(ProcessingAttempt.task_id == record.task_id)
        )
        return result.scalar_one_or_none()
