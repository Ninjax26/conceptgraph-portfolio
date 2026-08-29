from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, settings
from app.core.database import AsyncSessionLocal
from app.services.course_service import CourseService
from app.services.document_processing_service import document_processing_service
from app.services.storage_service import ObjectStorageError, StorageService, storage_service
from app.services.upload_service import UploadService

logger = logging.getLogger(__name__)


class DemoRetentionService:
    """Periodically remove expired shared-demo uploads from every backing store."""

    def __init__(
        self,
        *,
        config: Settings = settings,
        upload_service: UploadService | None = None,
        course_service: CourseService | None = None,
        storage: StorageService = storage_service,
    ) -> None:
        self.config = config
        self.upload_service = upload_service or UploadService()
        self.course_service = course_service or CourseService()
        self.storage = storage
        self.ingestion_service = document_processing_service.ingestion_service
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if not self.config.require_upload_auth or self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(
            self._cleanup_loop(),
            name="demo-upload-retention",
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def cleanup_once(self, *, now: datetime | None = None) -> int:
        if not self.config.require_upload_auth:
            return 0
        current_time = now or datetime.now(timezone.utc)
        cutoff = current_time - timedelta(days=self.config.demo_upload_retention_days)
        async with AsyncSessionLocal() as session:
            sample_course = None
            if self.config.public_sample_course_id:
                sample_course = await self.course_service.resolve(
                    session,
                    self.config.public_sample_course_id,
                    required=False,
                )
                if sample_course is None:
                    logger.warning(
                        "Skipping demo retention because public sample course %r "
                        "could not be resolved",
                        self.config.public_sample_course_id,
                    )
                    return 0
            upload_ids = await self.upload_service.list_expired_upload_ids(
                session,
                created_before=cutoff,
                excluded_course_uuid=sample_course.id if sample_course else None,
            )
            deleted = 0
            for upload_id in upload_ids:
                if await self._delete_upload(session, upload_id):
                    deleted += 1
            return deleted

    async def _delete_upload(self, session: AsyncSession, upload_id: str) -> bool:
        record = await self.upload_service.lock_for_deletion(session, upload_id)
        if record is None:
            await session.rollback()
            return False
        try:
            await self.ingestion_service.cleanup_upload(
                record.upload_id,
                record.course_uuid or record.course_id,
            )
            shared_object = await self.upload_service.storage_key_is_shared(
                session,
                record.storage_key,
                record.upload_id,
            )
            if not shared_object:
                await asyncio.to_thread(self.storage.delete, record.storage_key)
            deleted = await self.upload_service.delete_document(session, upload_id)
            return deleted is not None
        except ObjectStorageError:
            await session.rollback()
            logger.exception("Expired demo PDF storage cleanup failed upload=%s", upload_id)
        except Exception:
            await session.rollback()
            logger.exception("Expired demo upload cleanup failed upload=%s", upload_id)
        return False

    async def _cleanup_loop(self) -> None:
        while not self._stop.is_set():
            try:
                deleted = await self.cleanup_once()
                if deleted:
                    logger.info("Removed %s expired demo upload(s)", deleted)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Demo upload retention sweep failed")
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self.config.demo_cleanup_interval_seconds,
                )
            except TimeoutError:
                continue


demo_retention_service = DemoRetentionService()
