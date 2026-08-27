from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from enum import StrEnum
from uuid import uuid4

from app.core.config import Settings, settings
from app.core.database import AsyncSessionLocal
from app.services.document_processing_service import (
    DocumentProcessingService,
    document_processing_service,
)
from app.services.upload_service import UploadService

logger = logging.getLogger(__name__)


class EnqueueDisposition(StrEnum):
    ACCEPTED = "accepted"
    ALREADY_QUEUED = "already_queued"
    DEFERRED = "deferred"


@dataclass(frozen=True, slots=True)
class QueuedDocument:
    upload_id: str
    task_id: str

    @property
    def key(self) -> tuple[str, str]:
        return self.upload_id, self.task_id


class ProcessingCoordinator:
    """Bounded, lifecycle-owned dispatcher for in-process document work."""

    def __init__(
        self,
        *,
        config: Settings = settings,
        upload_service: UploadService | None = None,
        processing_service: DocumentProcessingService | None = None,
    ) -> None:
        self.config = config
        self.upload_service = upload_service or UploadService()
        self.processing_service = processing_service or document_processing_service
        self.instance_id = f"api-{uuid4()}"
        self.queue: asyncio.Queue[QueuedDocument] = asyncio.Queue(
            maxsize=config.processing_queue_capacity
        )
        self._queued: set[tuple[str, str]] = set()
        self._workers: list[asyncio.Task[None]] = []
        self._dispatcher: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._started = False

    @property
    def started(self) -> bool:
        return self._started

    @property
    def queue_depth(self) -> int:
        return self.queue.qsize()

    async def start(self) -> None:
        if self._started:
            return
        self._stop.clear()
        self._started = True
        self._workers = [
            asyncio.create_task(
                self._worker_loop(index),
                name=f"document-processor:{index}",
            )
            for index in range(self.config.processing_concurrency)
        ]
        await self._recover_and_fill()
        self._dispatcher = asyncio.create_task(
            self._dispatcher_loop(),
            name="document-dispatcher",
        )
        logger.info(
            "Processing coordinator started instance=%s concurrency=%s capacity=%s",
            self.instance_id,
            self.config.processing_concurrency,
            self.config.processing_queue_capacity,
        )

    async def stop(self) -> None:
        if not self._started:
            return
        self._stop.set()
        if self._dispatcher is not None:
            self._dispatcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._dispatcher
            self._dispatcher = None

        for worker in self._workers:
            worker.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        while not self.queue.empty():
            with contextlib.suppress(asyncio.QueueEmpty):
                self.queue.get_nowait()
                self.queue.task_done()
        self._queued.clear()
        self._started = False
        logger.info("Processing coordinator stopped instance=%s", self.instance_id)

    async def submit(
        self,
        upload_id: str,
        task_id: str,
    ) -> EnqueueDisposition:
        job = QueuedDocument(upload_id=upload_id, task_id=task_id)
        if job.key in self._queued:
            return EnqueueDisposition.ALREADY_QUEUED
        if not self._started or self.queue.full():
            return EnqueueDisposition.DEFERRED
        self.queue.put_nowait(job)
        self._queued.add(job.key)
        return EnqueueDisposition.ACCEPTED

    async def _dispatcher_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._recover_and_fill()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Processing dispatcher sweep failed")
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self.config.processing_dispatch_interval_seconds,
                )
            except TimeoutError:
                continue

    async def _recover_and_fill(self) -> None:
        available = self.config.processing_queue_capacity - self.queue.qsize()
        if available <= 0:
            return
        async with AsyncSessionLocal() as session:
            recovered = await self.upload_service.prepare_stale_recoveries(session)
            if recovered:
                logger.warning(
                    "Prepared %s interrupted document execution(s) for recovery",
                    len(recovered),
                )
            records = await self.upload_service.list_dispatchable(
                session,
                limit=available,
            )
        for record in records:
            disposition = await self.submit(record.upload_id, record.task_id)
            if disposition == EnqueueDisposition.DEFERRED:
                break

    async def _worker_loop(self, worker_index: int) -> None:
        while True:
            job = await self.queue.get()
            try:
                async with AsyncSessionLocal() as session:
                    claimed = await self.upload_service.claim_for_processing(
                        session,
                        upload_id=job.upload_id,
                        task_id=job.task_id,
                        lease_owner=self.instance_id,
                        lease_seconds=self.config.processing_lease_seconds,
                    )
                if claimed is None:
                    continue
                logger.info(
                    "Processor %s claimed upload=%s task=%s",
                    worker_index,
                    job.upload_id,
                    job.task_id,
                )
                await self.processing_service.process_document(
                    job.upload_id,
                    job.task_id,
                    lease_owner=self.instance_id,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Unhandled processing coordinator error upload=%s task=%s",
                    job.upload_id,
                    job.task_id,
                )
            finally:
                self._queued.discard(job.key)
                self.queue.task_done()


processing_coordinator = ProcessingCoordinator()
