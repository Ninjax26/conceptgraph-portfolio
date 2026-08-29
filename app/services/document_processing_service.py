from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, settings
from app.core.database import AsyncSessionLocal
from app.core.processing import ProcessingStage, assess_graph_status, classify_failure
from app.models.document_upload import DocumentUpload
from app.services.ingestion_service import IngestionService
from app.services.parser_service import ParserService
from app.services.storage_service import storage_service
from app.services.upload_service import UploadService

logger = logging.getLogger(__name__)
T = TypeVar("T")


class ProcessingAttemptSuperseded(RuntimeError):
    pass


class DocumentProcessingService:
    """Framework-independent, lease-fenced PDF processing pipeline."""

    def __init__(
        self,
        *,
        config: Settings = settings,
        ingestion_service: IngestionService | None = None,
        parser_service: ParserService | None = None,
        upload_service: UploadService | None = None,
    ) -> None:
        self.config = config
        self.ingestion_service = ingestion_service or IngestionService()
        self.parser_service = parser_service or ParserService()
        self.upload_service = upload_service or UploadService()

    async def process_document(
        self,
        upload_id: str,
        task_id: str,
        *,
        lease_owner: str,
    ) -> dict[str, int | str]:
        record = await self._run_with_session(
            lambda session: self.upload_service.get_upload(session, upload_id)
        )
        if (
            record is None
            or record.task_id != task_id
            or record.lease_owner != lease_owner
            or record.status != "active"
        ):
            raise ProcessingAttemptSuperseded(
                f"Processing attempt {task_id} is not claimed for {upload_id}."
            )
        heartbeat_stop = asyncio.Event()
        superseded = asyncio.Event()
        heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(
                upload_id,
                task_id,
                lease_owner,
                heartbeat_stop,
                superseded,
            ),
            name=f"document-heartbeat:{upload_id}",
        )
        try:
            if not record.course_uuid:
                raise ValueError("The document is not associated with a canonical course.")
            await self._ensure_current(superseded, upload_id, task_id)

            # Every execution starts from a clean provenance scope. This makes
            # retry and restart recovery idempotent after partial external writes.
            await self.ingestion_service.cleanup_upload(upload_id, record.course_uuid)
            await self._set_stage(
                upload_id, task_id, lease_owner, ProcessingStage.EXTRACTING
            )
            pdf_content = await asyncio.to_thread(
                storage_service.get_bytes, record.storage_key
            )
            pages = self.parser_service.extract_pages_from_bytes(pdf_content)
            await self._set_stage(
                upload_id, task_id, lease_owner, ProcessingStage.EXTRACTED
            )

            await self._set_stage(
                upload_id, task_id, lease_owner, ProcessingStage.CHUNKING
            )
            chunks = self.parser_service.chunk_pages(
                pages,
                record.course_uuid,
                upload_id,
                record.original_filename,
            )
            for chunk in chunks:
                chunk.metadata["execution_token"] = task_id
            if not chunks:
                raise ValueError(
                    "No extractable text was found in this PDF. "
                    "Scanned PDFs need OCR before ingestion."
                )
            await self._set_stage(
                upload_id, task_id, lease_owner, ProcessingStage.CHUNKED
            )

            await self._set_stage(
                upload_id, task_id, lease_owner, ProcessingStage.EMBEDDING
            )
            vector_count = await asyncio.to_thread(
                self.ingestion_service.upsert_chunks_to_qdrant, chunks
            )
            if vector_count != len(chunks) or vector_count <= 0:
                raise RuntimeError("Qdrant did not commit every document chunk.")
            await self._ensure_current(superseded, upload_id, task_id)
            await self._set_stage(
                upload_id, task_id, lease_owner, ProcessingStage.EMBEDDED
            )

            await self._set_stage(
                upload_id, task_id, lease_owner, ProcessingStage.BUILDING_GRAPH
            )
            graph = await self.ingestion_service.extract_graph_from_chunks(chunks)
            await self._ensure_current(superseded, upload_id, task_id)
            await self.ingestion_service.store_graph_extraction(
                graph,
                record.course_uuid,
                upload_id=upload_id,
                document_name=record.original_filename,
                course_name=record.course_id,
                execution_token=task_id,
            )
            await self._set_stage(
                upload_id, task_id, lease_owner, ProcessingStage.GRAPH_BUILT
            )

            source_exists = await asyncio.to_thread(
                storage_service.exists, record.storage_key
            )
            if not source_exists:
                raise FileNotFoundError("The source PDF object was not found.")

            result: dict[str, Any] = {
                "chunks_indexed": vector_count,
                "nodes_upserted": len(graph.nodes),
                "relationships_upserted": len(graph.relationships),
                "graph_status": assess_graph_status(
                    len(graph.nodes),
                    len(graph.relationships),
                ).value,
            }
            completed = await self._run_with_session(
                lambda session: self.upload_service.mark_completed(
                    session,
                    upload_id,
                    task_id,
                    result,
                    lease_owner=lease_owner,
                )
            )
            if not completed:
                raise ProcessingAttemptSuperseded(
                    f"Processing attempt {task_id} is no longer current for {upload_id}."
                )
            return {
                "upload_id": upload_id,
                "course_id": record.course_uuid,
                "status": "ready",
                **result,
            }
        except ProcessingAttemptSuperseded:
            logger.info(
                "Stopped superseded processing attempt %s for %s", task_id, upload_id
            )
            return {
                "upload_id": upload_id,
                "course_id": record.course_uuid,
                "status": "superseded",
            }
        except asyncio.CancelledError:
            await self._run_with_session(
                lambda session: self.upload_service.release_lease(
                    session, upload_id, task_id, lease_owner
                )
            )
            raise
        except Exception as exc:
            category, retryable, message = classify_failure(exc)
            logger.exception("PDF processing failed for upload %s", upload_id)
            current = await self._run_with_session(
                lambda session: self.upload_service.heartbeat(
                    session,
                    upload_id,
                    task_id,
                    lease_owner=lease_owner,
                    lease_seconds=self.config.processing_lease_seconds,
                )
            )
            if current:
                try:
                    await self.ingestion_service.cleanup_upload(
                        upload_id, record.course_uuid
                    )
                except Exception:
                    logger.exception(
                        "Partial-write cleanup failed for upload %s", upload_id
                    )
                await self._run_with_session(
                    lambda session: self.upload_service.mark_failed(
                        session,
                        upload_id,
                        task_id,
                        message,
                        category,
                        retryable,
                        lease_owner=lease_owner,
                    )
                )
            return {
                "upload_id": upload_id,
                "course_id": record.course_uuid,
                "status": "failed",
                "error": message,
            }
        finally:
            heartbeat_stop.set()
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat_task

    async def _set_stage(
        self,
        upload_id: str,
        task_id: str,
        lease_owner: str,
        stage: ProcessingStage,
    ) -> None:
        updated = await self._run_with_session(
            lambda session: self.upload_service.set_stage(
                session,
                upload_id,
                task_id,
                stage,
                lease_owner=lease_owner,
                lease_seconds=self.config.processing_lease_seconds,
            )
        )
        if not updated:
            raise ProcessingAttemptSuperseded(
                f"Processing attempt {task_id} is no longer current for {upload_id}."
            )

    async def _heartbeat_loop(
        self,
        upload_id: str,
        task_id: str,
        lease_owner: str,
        stop: asyncio.Event,
        superseded: asyncio.Event,
    ) -> None:
        while not stop.is_set():
            try:
                current = await self._run_with_session(
                    lambda session: self.upload_service.heartbeat(
                        session,
                        upload_id,
                        task_id,
                        lease_owner=lease_owner,
                        lease_seconds=self.config.processing_lease_seconds,
                    )
                )
                if not current:
                    superseded.set()
                    return
            except Exception:
                logger.exception("Heartbeat failed for upload %s", upload_id)
            try:
                await asyncio.wait_for(
                    stop.wait(), timeout=self.config.processing_heartbeat_seconds
                )
            except TimeoutError:
                continue

    @staticmethod
    async def _ensure_current(
        superseded: asyncio.Event,
        upload_id: str,
        task_id: str,
    ) -> None:
        if superseded.is_set():
            raise ProcessingAttemptSuperseded(
                f"Processing attempt {task_id} was superseded for {upload_id}."
            )

    @staticmethod
    async def _run_with_session(
        operation: Callable[[AsyncSession], Awaitable[T]],
    ) -> T:
        async with AsyncSessionLocal() as session:
            return await operation(session)


document_processing_service = DocumentProcessingService()
