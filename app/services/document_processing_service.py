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
from app.core.blocking import run_blocking_safely
from app.core.database import AsyncSessionLocal
from app.core.processing import ProcessingStage, assess_graph_status, classify_failure
from app.schemas.extraction import GraphExtractionResponse
from app.services.ingestion_service import GraphSectionBatch, IngestionService
from app.services.parser_service import ExtractedPage, ParserService
from app.services.storage_service import storage_service
from app.services.upload_service import UploadService
from app.services.shared_provider_service import provider_call_counts

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
                            GraphExtractionResponse.model_validate(
                                checkpoint.extraction_json
                            )
                        )
                    except (ValueError, TypeError):
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
            cleanup_graph = getattr(self.ingestion_service, "cleanup_graph", None)
            if cleanup_graph is not None:
                await cleanup_graph(upload_id, record.course_uuid)
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
            stage_seconds["graph_building"] = round(time.perf_counter() - stage_started, 4)

            source_exists = await run_blocking_safely(
                storage_service.exists, record.storage_key
            )
            if not source_exists:
                raise FileNotFoundError("The source PDF object was not found.")

            graph_status = assess_graph_status(
                len(graph.nodes),
                len(graph.relationships),
                sections_total=graph.sections_total,
                sections_succeeded=graph.sections_succeeded,
                sections_empty=graph.sections_empty,
                batches_failed=graph.batches_failed,
                batches_skipped=graph.batches_skipped,
            ).value
            result: dict[str, Any] = {
                "chunks_indexed": vector_count,
                "chunks_total": len(chunks),
                "sections_scanned_locally": graph.sections_total,
                "nodes_upserted": len(graph.nodes),
                "relationships_upserted": len(graph.relationships),
                "graph_status": graph_status,
                "graph_sections_total": graph.sections_total,
                "graph_sections_succeeded": graph.sections_succeeded,
                "graph_sections_failed": graph.sections_failed,
                "graph_sections_processed": graph.sections_processed,
                "graph_sections_empty": graph.sections_empty,
                "graph_batches_empty": graph.batches_empty,
                "graph_batches_total": graph.batches_total,
                "graph_batches_succeeded": graph.batches_succeeded,
                "graph_batches_failed": graph.batches_failed,
                "graph_batches_skipped": graph.batches_skipped,
                "graph_provider_limited": graph.provider_limited,
                "graph_extraction_budget_applied": graph.extraction_budget_applied,
                "graph_global_linking_attempted": graph.global_linking_attempted,
                "graph_global_linking_succeeded": graph.global_linking_succeeded,
                "graph_global_linking_processed": graph.global_linking_processed,
                "graph_relationship_pass_version": 4 if graph.global_linking_attempted else 0,
                "graph_checkpointed_batches": graph.batches_succeeded,
                "graph_failed_sections": graph.failed_section_labels,
                "graph_batch_diagnostics": [entry.model_dump(mode="json") for entry in graph.batch_diagnostics],
                **extraction_summary,
                "processing_seconds": round(time.perf_counter() - processing_started, 4),
                "stage_seconds": stage_seconds,
                "source_bytes": len(pdf_content),
                "page_count": len(pages),
                "graph_provider_calls": (provider_call_counts.get() or {}).get("graph_extraction", 0),
                "queue_wait_seconds": max(0, (record.started_at - record.last_attempted_at).total_seconds())
                if getattr(record, "started_at", None) is not None and getattr(record, "last_attempted_at", None) is not None else None,
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
