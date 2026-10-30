from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from enum import StrEnum
from uuid import uuid4

from app.core.blocking import run_async_safely
from app.core.config import Settings, settings
from app.core.database import AsyncSessionLocal
from app.services.document_processing_service import (
    DocumentProcessingService,
    document_processing_service,
)
from app.services.upload_service import UploadService
from app.services.graph_only_processor import GraphOnlyProcessor
from app.services.graph_job_maintenance import GraphJobMaintenance
from app.services.graph_job_extraction import GraphJobDeferred, GraphJobAuthorityLost

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
        graph_processor: GraphOnlyProcessor | None = None,
    ) -> None:
        self.config = config
        self.upload_service = upload_service or UploadService()
        self.processing_service = processing_service or document_processing_service
        self._graph_processor = graph_processor
        self._graph_maintenance: GraphJobMaintenance | None = None
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

    def _graph_dispatch_enabled(self) -> bool:
        config = self.config
        return (
            config.graph_continuation_dispatch_enabled is True
            and config.graph_continuation_snapshots_enabled is True
            and config.graph_projection_fencing_enabled is True
            and config.shared_provider_limits_enabled is True
            and config.processing_role in {"combined", "worker"}
            and all(
                getattr(config, f"provider_interactive_request_reserve_per_{period}") > 0
                and (not getattr(config, f"provider_estimated_tokens_per_{period}")
                     or getattr(config, f"provider_interactive_token_reserve_per_{period}") > 0)
                for period in ("minute", "day")
            )
        )

    async def _process_due_graph_job(self) -> bool:
        """Use one idle worker slot; never queue a claimed lease or retry inline.

        Headroom protects provider admission, not preemption of an in-flight
        graph request. A document arriving after claiming waits for that slot.
        Other failures retain the bounded job lease for later recovery.
        """
        if self._stop.is_set() or not self.queue.empty() or not self._graph_dispatch_enabled():
            return False
        if self._graph_processor is None:
            self._graph_processor = GraphOnlyProcessor(AsyncSessionLocal,
                self.processing_service.ingestion_service, config=self.config)
        processor = self._graph_processor
        if self.config.graph_continuation_maintenance_enabled is True:
            if self._graph_maintenance is None:
                self._graph_maintenance = GraphJobMaintenance(AsyncSessionLocal,
                    self.processing_service.ingestion_service, config=self.config, jobs=processor.jobs)
            await self._graph_maintenance.run_if_due()
            if self._stop.is_set() or not self.queue.empty():
                return False

        async def claim_due():
            async with AsyncSessionLocal() as session:
                return await processor.jobs.claim_next(session, lease_owner=self.instance_id,
                    lease_seconds=self.config.processing_lease_seconds)

        # A cancelled claim commit must settle before this worker can stop.
        # If it committed, an unstarted claim recovers by its existing lease.
        claim = await run_async_safely(claim_due())
        if claim is None:
            return False
        try:
            return await processor.process(claim, lease_seconds=self.config.processing_lease_seconds)
        except GraphJobDeferred:
            logger.info("Graph job postponed until its persisted provider deadline")
        except GraphJobAuthorityLost:
            logger.info("Graph job authority changed before completion")
        return False

    async def _worker_loop(self, worker_index: int) -> None:
        while True:
            if self._graph_dispatch_enabled():
                try:
                    job = await asyncio.wait_for(self.queue.get(),
                        timeout=self.config.processing_dispatch_interval_seconds)
                except TimeoutError:
                    try:
                        await self._process_due_graph_job()
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception("Idle graph processing failed; retained lease recovery applies")
                    continue
            else:
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
