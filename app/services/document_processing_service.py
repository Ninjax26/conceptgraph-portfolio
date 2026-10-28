from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from collections.abc import Sequence
from typing import Any, TypeVar

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, settings
from app.core.exceptions import GraphStructureError
from app.core.blocking import run_async_safely, run_blocking_safely
from app.core.database import AsyncSessionLocal
from app.core.processing import ProcessingStage, classify_failure
from app.models.document_upload import DocumentUpload
from app.schemas.extraction import GraphExtractionResponse
from app.services.ingestion_service import GraphSectionBatch, IngestionService
from app.services.graph_result_metadata import graph_result_metadata
from app.services.parser_service import ExtractedPage, ParserService
from app.services.storage_service import storage_service
from app.services.upload_service import UploadService
from app.services.shared_provider_service import provider_call_counts, reserve_provider_request
from app.services.provider_budget_policy import estimated_reservation

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
        self.parser_service = parser_service or ParserService(
            ocr_enabled=config.ocr_enabled,
            ocr_language=config.ocr_language,
            ocr_dpi=config.ocr_dpi,
            ocr_min_native_characters=config.ocr_min_native_characters,
            ocr_max_pages_per_document=config.ocr_max_pages_per_document,
            ocr_page_timeout_seconds=config.ocr_page_timeout_seconds,
        )
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
        processing_started = time.perf_counter()
        call_token = provider_call_counts.set({})
        stage_seconds: dict[str, float] = {}
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

            cleanup_vectors = getattr(self.ingestion_service, "cleanup_vectors", None)
            if cleanup_vectors is not None:
                if self.config.graph_projection_fencing_enabled:
                    await self._with_current_attempt(upload_id, task_id, lease_owner,
                        lambda session, current: run_blocking_safely(cleanup_vectors, upload_id))
                else:
                    await run_blocking_safely(cleanup_vectors, upload_id)

            await self._set_stage(
                upload_id, task_id, lease_owner, ProcessingStage.EXTRACTING
            )
            stage_started = time.perf_counter()
            pdf_content = await run_blocking_safely(
                storage_service.get_bytes, record.storage_key
            )
            pages = await run_blocking_safely(
                self.parser_service.extract_pages_from_bytes,
                pdf_content,
            )
            extraction_summary = self._page_extraction_summary(pages)
            stage_seconds["text_extraction"] = round(time.perf_counter() - stage_started, 4)
            await self._set_stage(
                upload_id, task_id, lease_owner, ProcessingStage.EXTRACTED
            )

            await self._set_stage(
                upload_id, task_id, lease_owner, ProcessingStage.CHUNKING
            )
            chunks = await run_blocking_safely(
                self.parser_service.chunk_pages,
                pages,
                record.course_uuid,
                upload_id,
                record.original_filename,
                max_chunks=self.config.max_chunks_per_document,
            )
            for chunk in chunks:
                chunk.metadata["execution_token"] = task_id
            if not chunks:
                raise ValueError(
                    "No readable text was found after native extraction and OCR."
                )
            reserve = getattr(self.upload_service, "reserve_chunks", None)
            if reserve is not None:
                reserved = await self._run_with_session(lambda session: reserve(
                    session, upload_id, task_id, lease_owner=lease_owner,
                    pages=len(pages), chunks=len(chunks), source_bytes=len(pdf_content)))
                if not reserved:
                    raise ProcessingAttemptSuperseded("Resource reservation belongs to a superseded attempt.")
            await self._set_stage(
                upload_id, task_id, lease_owner, ProcessingStage.CHUNKED
            )

            await self._set_stage(
                upload_id, task_id, lease_owner, ProcessingStage.EMBEDDING
            )
            stage_started = time.perf_counter()
            if settings.embedding_provider == "qdrant_cloud":
                await reserve_provider_request(
                    "qdrant_cloud",
                    operation="document_embedding",
                    estimated_tokens=estimated_reservation(
                        sum(len(chunk.text) for chunk in chunks), "document_embedding"
                    ),
                )
            if self.config.graph_projection_fencing_enabled:
                vector_count = await self._with_current_attempt(upload_id, task_id, lease_owner,
                    lambda session, current: run_blocking_safely(self.ingestion_service.upsert_chunks_to_qdrant, chunks))
            else:
                vector_count = await run_blocking_safely(
                    self.ingestion_service.upsert_chunks_to_qdrant, chunks
                )
            if vector_count != len(chunks) or vector_count <= 0:
                raise RuntimeError("Qdrant did not commit every document chunk.")
            stage_seconds["vector_indexing"] = round(time.perf_counter() - stage_started, 4)
            await self._ensure_current(superseded, upload_id, task_id)
            await self._set_stage(
                upload_id, task_id, lease_owner, ProcessingStage.EMBEDDED
            )

            await self._set_stage(
                upload_id, task_id, lease_owner, ProcessingStage.BUILDING_GRAPH
            )
            stage_started = time.perf_counter()
            completed_batches: dict[str, GraphExtractionResponse] = {}
            load_checkpoints = getattr(
                self.upload_service, "load_graph_checkpoints", None
            )
            if load_checkpoints is not None:
                checkpoint_rows = await self._run_with_session(
                    lambda session: load_checkpoints(session, upload_id)
                )
                for checkpoint in checkpoint_rows:
                    try:
                        completed_batches[checkpoint.batch_key] = (
                            IngestionService.decode_graph_checkpoint(
                                checkpoint.extraction_json
                            )
                        )
                    except (GraphStructureError, ValueError, TypeError, KeyError):
                        logger.warning(
                            "Ignored invalid graph checkpoint %s for upload %s",
                            checkpoint.batch_key,
                            upload_id,
                        )

            async def save_checkpoint(
                batch: GraphSectionBatch,
                extraction: GraphExtractionResponse,
            ) -> None:
                save_graph_checkpoint = getattr(
                    self.upload_service, "save_graph_checkpoint", None
                )
                if save_graph_checkpoint is None:
                    return
                saved = await self._run_with_session(
                    lambda session: save_graph_checkpoint(
                        session,
                        upload_id=upload_id,
                        task_id=task_id,
                        lease_owner=lease_owner,
                        batch_key=batch.batch_key,
                        section_key=batch.section_key,
                        section_label=batch.section_label,
                        extraction_json=extraction.model_dump(mode="json"),
                    )
                )
                if not saved:
                    raise ProcessingAttemptSuperseded(
                        f"Processing attempt {task_id} is no longer current for {upload_id}."
                    )

            graph = await self.ingestion_service.extract_graph_from_chunks(
                chunks,
                completed_batches=completed_batches,
                on_batch_completed=save_checkpoint,
            )
            await self._ensure_current(superseded, upload_id, task_id)
            if self.config.graph_projection_fencing_enabled:
                async def replace_current(session, current):
                    await self.ingestion_service.replace_graph_extraction(graph, current.course_uuid,
                        upload_id=upload_id, document_name=current.original_filename,
                        course_name=current.course_id, execution_token=task_id,
                        source_generation=current.attempt_count)
                await self._with_current_attempt(upload_id, task_id, lease_owner, replace_current)
            else:
                cleanup_graph = getattr(self.ingestion_service, "cleanup_graph", None)
                if cleanup_graph is not None:
                    await cleanup_graph(upload_id, record.course_uuid)
                await self.ingestion_service.store_graph_extraction(
                    graph, record.course_uuid, upload_id=upload_id,
                    document_name=record.original_filename, course_name=record.course_id,
                    execution_token=task_id,
                )
            await self._set_stage(
                upload_id, task_id, lease_owner, ProcessingStage.GRAPH_BUILT
            )
            stage_seconds["graph_building"] = round(time.perf_counter() - stage_started, 4)

            source_exists = await run_blocking_safely(
                storage_service.exists, record.storage_key
            )
            if not source_exists:
                raise FileNotFoundError("The source PDF object was not found.")

            graph_metadata = graph_result_metadata(graph)
            graph_status = graph_metadata["graph_status"]
            result: dict[str, Any] = {
                "chunks_indexed": vector_count,
                "chunks_total": len(chunks),
                **graph_metadata,
                **extraction_summary,
                "processing_seconds": round(time.perf_counter() - processing_started, 4),
                "stage_seconds": stage_seconds,
                "source_bytes": len(pdf_content),
                "page_count": len(pages),
                "graph_provider_calls": (provider_call_counts.get() or {}).get("graph_extraction", 0),
                "queue_wait_seconds": max(0, (record.started_at - record.last_attempted_at).total_seconds())
                if getattr(record, "started_at", None) is not None and getattr(record, "last_attempted_at", None) is not None else None,
            }
            if graph.provider_retry_after_seconds is not None:
                result["graph_retry_after_seconds"] = graph.provider_retry_after_seconds
            continuation_options = (
                {"graph_continuation_chunks": chunks}
                if self.config.graph_continuation_snapshots_enabled
                and graph.provider_limited and graph.provider_retry_after_seconds is not None
                else {}
            )
            completed = await self._run_with_session(
                lambda session: self.upload_service.mark_completed(
                    session,
                    upload_id,
                    task_id,
                    result,
                    lease_owner=lease_owner,
                    **continuation_options,
                )
            )
            if not completed:
                raise ProcessingAttemptSuperseded(
                    f"Processing attempt {task_id} is no longer current for {upload_id}."
                )
            if graph_status == "GRAPH_READY":
                clear_checkpoints = getattr(
                    self.upload_service, "clear_graph_checkpoints", None
                )
                if clear_checkpoints is not None:
                    try:
                        await self._run_with_session(
                            lambda session: clear_checkpoints(session, upload_id)
                        )
                    except Exception:
                        logger.exception(
                            "Could not remove completed graph checkpoints for %s",
                            upload_id,
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
            if self.config.graph_projection_fencing_enabled:
                async def fail_current(session, current):
                    try:
                        await self.ingestion_service.cleanup_graph_attempt(upload_id,
                            current.course_uuid or current.course_id,
                            execution_token=task_id, source_generation=current.attempt_count)
                        await run_blocking_safely(self.ingestion_service.cleanup_vectors,
                            upload_id, execution_token=task_id)
                    except Exception:
                        logger.exception("Partial-write cleanup failed for upload %s", upload_id)
                    await self.upload_service.mark_failed(session, upload_id, task_id,
                        message, category, retryable, lease_owner=lease_owner)
                try:
                    await self._with_current_attempt(upload_id, task_id, lease_owner, fail_current)
                except ProcessingAttemptSuperseded:
                    logger.info("Skipped cleanup for superseded attempt %s", task_id)
                return {"upload_id": upload_id, "course_id": record.course_uuid,
                        "status": "failed", "error": message}
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
            provider_call_counts.reset(call_token)
            heartbeat_stop.set()
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat_task

    async def _with_current_attempt(
        self, upload_id: str, task_id: str, lease_owner: str,
        operation: Callable[[AsyncSession, DocumentUpload], Awaitable[T]],
    ) -> T:
        """Retain a fresh source/task/owner row lock through a drained I/O unit.

        Existing upload helpers own any stage/failure commit. Read-only units
        roll back when the session closes; no lease or task is fabricated here.
        """
        async def guarded():
            async with AsyncSessionLocal() as session:
                current = await self.upload_service._get_current_upload(
                    session, upload_id, task_id, lease_owner=lease_owner)
                if current is None or current.status != "active" or UploadService.is_deleting(current):
                    raise ProcessingAttemptSuperseded(f"Processing attempt {task_id} is no longer current for {upload_id}.")
                return await operation(session, current)
        return await run_async_safely(guarded())

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
    def _page_extraction_summary(
        pages: Sequence[ExtractedPage | tuple[int, str]],
    ) -> dict[str, Any]:
        native_pages = 0
        ocr_pages = 0
        hybrid_pages = 0
        warning_pages = 0
        ocr_attempts = 0
        unreadable_pages = 0
        warning_page_numbers: list[int] = []
        confidences: list[float] = []
        for page in pages:
            if isinstance(page, ExtractedPage):
                ocr_attempts += page.ocr_attempts
                warning_pages += bool(page.extraction_warnings)
                if page.extraction_warnings:
                    warning_page_numbers.append(page.page_number)
            if isinstance(page, ExtractedPage) and page.extraction_method in {"ocr", "native_text+ocr"}:
                if page.extraction_method == "native_text+ocr":
                    hybrid_pages += 1
                else:
                    ocr_pages += 1
                if page.ocr_confidence is not None:
                    confidences.append(page.ocr_confidence)
            elif isinstance(page, ExtractedPage) and page.extraction_method == "unreadable":
                unreadable_pages += 1
            else:
                native_pages += 1
        return {
            "pages_extracted": len(pages),
            "native_text_pages": native_pages,
            "ocr_pages": ocr_pages,
            "hybrid_text_ocr_pages": hybrid_pages,
            "extraction_warning_pages": warning_pages,
            "ocr_attempts": ocr_attempts,
            "unreadable_pages": unreadable_pages,
            "extraction_warning_page_numbers": warning_page_numbers,
            "ocr_average_confidence": (
                round(sum(confidences) / len(confidences), 1)
                if confidences
                else None
            ),
        }

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
