from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from collections.abc import Sequence
from typing import Any, TypeVar

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, settings
from app.core.database import AsyncSessionLocal
from app.core.processing import ProcessingStage, assess_graph_status, classify_failure
from app.models.document_upload import DocumentUpload
from app.schemas.extraction import GraphExtractionResponse
from app.services.ingestion_service import GraphSectionBatch, IngestionService
from app.services.parser_service import ExtractedPage, ParserService
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
                await asyncio.to_thread(cleanup_vectors, upload_id)

            await self._set_stage(
                upload_id, task_id, lease_owner, ProcessingStage.EXTRACTING
            )
            pdf_content = await asyncio.to_thread(
                storage_service.get_bytes, record.storage_key
            )
            pages = await asyncio.to_thread(
                self.parser_service.extract_pages_from_bytes,
                pdf_content,
            )
            extraction_summary = self._page_extraction_summary(pages)
            await self._set_stage(
                upload_id, task_id, lease_owner, ProcessingStage.EXTRACTED
            )

            await self._set_stage(
                upload_id, task_id, lease_owner, ProcessingStage.CHUNKING
            )
            chunks = await asyncio.to_thread(
                self.parser_service.chunk_pages,
                pages,
                record.course_uuid,
                upload_id,
                record.original_filename,
            )
            for chunk in chunks:
                chunk.metadata["execution_token"] = task_id
            if not chunks:
                raise ValueError(
                    "No readable text was found after native extraction and OCR."
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

            source_exists = await asyncio.to_thread(
                storage_service.exists, record.storage_key
            )
            if not source_exists:
                raise FileNotFoundError("The source PDF object was not found.")

            graph_status = assess_graph_status(
                len(graph.nodes),
                len(graph.relationships),
                sections_total=graph.sections_total,
                sections_succeeded=graph.sections_succeeded,
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
                "graph_batches_total": graph.batches_total,
                "graph_batches_succeeded": graph.batches_succeeded,
                "graph_batches_failed": graph.batches_failed,
                "graph_batches_skipped": graph.batches_skipped,
                "graph_provider_limited": graph.provider_limited,
                "graph_extraction_budget_applied": graph.extraction_budget_applied,
                "graph_global_linking_attempted": graph.global_linking_attempted,
                "graph_global_linking_succeeded": graph.global_linking_succeeded,
                "graph_checkpointed_batches": graph.batches_succeeded,
                "graph_failed_sections": graph.failed_section_labels,
                **extraction_summary,
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
    ) -> dict[str, int | float | None]:
        native_pages = 0
        ocr_pages = 0
        confidences: list[float] = []
        for page in pages:
            if isinstance(page, ExtractedPage) and page.extraction_method == "ocr":
                ocr_pages += 1
                if page.ocr_confidence is not None:
                    confidences.append(page.ocr_confidence)
            else:
                native_pages += 1
        return {
            "pages_extracted": len(pages),
            "native_text_pages": native_pages,
            "ocr_pages": ocr_pages,
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
