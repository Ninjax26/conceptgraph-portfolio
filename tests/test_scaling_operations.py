"""Budget, paging, worker-role and cancellation boundary regressions."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import AsyncMock, Mock, patch

from sqlalchemy.dialects import postgresql

from app.core.blocking import run_blocking_safely
from app.core.config import Settings
from app.core.exceptions import LLMProviderRateLimitError, LLMProviderUnavailableError
from app.services.graph_display_service import bound_graph_context
from app.services.resource_budget_service import ResourceBudgetExceeded, ResourceBudgetService, ResourceUsage
from app.services.upload_pagination import decode_upload_cursor, encode_upload_cursor, paginated_uploads
from scripts.evaluate_ingestion_scaling import require_local_database


class ResourceBudgetTests(unittest.TestCase):
    def setUp(self):
        self.config = Settings(_env_file=None, MAX_PDFS_PER_INSTALLATION=0, MAX_PAGES_PER_DOCUMENT=10,
                               MAX_PAGES_PER_INSTALLATION=20, MAX_CHUNKS_PER_DOCUMENT=100,
                               MAX_CHUNKS_PER_INSTALLATION=200, MAX_STORAGE_MB=1, MAX_PENDING_DOCUMENTS=2)
        self.service = ResourceBudgetService(self.config)

    def test_zero_legacy_cap_uses_resource_budgets_not_document_count(self):
        self.service.check(ResourceUsage(documents=1000), pages=1, chunks=1, source_bytes=10)

    def test_each_installation_resource_limit_is_enforced(self):
        for usage in (ResourceUsage(pages=20), ResourceUsage(chunks=200),
                      ResourceUsage(source_bytes=1024 * 1024), ResourceUsage(pending=2)):
            with self.subTest(usage=usage), self.assertRaises(ResourceBudgetExceeded):
                self.service.check(usage, pages=1, chunks=1, source_bytes=1)

    def test_document_page_and_chunk_limits_are_enforced(self):
        for pages, chunks in ((11, 1), (1, 101)):
            with self.assertRaises(ResourceBudgetExceeded):
                self.service.check(ResourceUsage(), pages=pages, chunks=chunks, source_bytes=1)

    def test_reservation_is_bounded_and_actual_chunk_reconciliation_does_not_add_a_pending_job(self):
        self.assertEqual(self.service.chunk_reservation(10), 100)
        self.service.check(ResourceUsage(pending=2), pages=1, chunks=1, source_bytes=1, new_document=False)

    def test_legacy_count_limit_remains_an_optional_compatibility_guard(self):
        self.config.max_pdfs_per_installation = 1
        with self.assertRaises(ResourceBudgetExceeded):
            self.service.check(ResourceUsage(documents=1), pages=1, chunks=1, source_bytes=1)

    def test_shared_capacity_exhaustion_can_retry_but_document_overflow_cannot(self):
        from app.core.processing import classify_failure
        self.assertTrue(classify_failure(ResourceBudgetExceeded("Capacity full"))[1])
        self.assertFalse(classify_failure(ResourceBudgetExceeded("Split this PDF", retryable=False))[1])

    def test_fixture_benchmark_rejects_remote_database(self):
        require_local_database("postgresql+asyncpg://localhost/fixture")
        for url in ("postgresql://remote.example/production", "postgresql://localhost.example/production"):
            with self.assertRaises(ValueError):
                require_local_database(url)

    def test_streamed_chunks_preserve_overlap_and_exact_final_boundary(self):
        import re
        from app.services.parser_service import ParserService
        parser = ParserService(chunk_size=5, chunk_overlap=2)
        for size in range(16):
            text = " \n ".join(f"word{index}" for index in range(size))
            words = list(re.finditer(r"\S+", text))
            expected = []
            for start in range(0, len(words), 3):
                end = min(start + 5, len(words))
                expected.append(text[words[start].start():words[end - 1].end()].strip())
                if end == len(words):
                    break
            self.assertEqual(parser._split_text(text), expected)

    def test_chunk_limit_stops_before_building_the_unbounded_result(self):
        from app.services.parser_service import ParserService
        parser = ParserService(chunk_size=5, chunk_overlap=2)
        with self.assertRaises(ResourceBudgetExceeded):
            parser.chunk_pages([(1, "word " * 1000)], "course", "upload", max_chunks=2)
        with self.assertRaises(ValueError):
            parser.chunk_pages([], "course", "upload", max_chunks=0)


class ModelRuntimeTests(unittest.TestCase):
    def test_forced_cpu_does_not_import_an_accelerator_runtime(self):
        from app.core.model_runtime import local_model_device
        with patch("app.core.config.settings.local_model_device", "cpu"), patch.dict("sys.modules", {"torch": None}):
            self.assertEqual(local_model_device(), "cpu")

    def test_auto_preserves_existing_device_selection(self):
        from app.core.model_runtime import local_model_device
        for available, expected in ((True, "mps"), (False, "cpu")):
            torch = SimpleNamespace(backends=SimpleNamespace(mps=SimpleNamespace(is_available=lambda: available)))
            with patch("app.core.config.settings.local_model_device", "auto"), patch.dict("sys.modules", {"torch": torch}):
                self.assertEqual(local_model_device(), expected)

    def test_unavailable_requested_mps_is_a_clear_configuration_error(self):
        from app.core.model_runtime import local_model_device
        torch = SimpleNamespace(backends=SimpleNamespace(mps=SimpleNamespace(is_available=lambda: False)))
        with patch("app.core.config.settings.local_model_device", "mps"), patch.dict("sys.modules", {"torch": torch}):
            with self.assertRaisesRegex(RuntimeError, "Apple Metal"):
                local_model_device()

    def test_all_local_model_clients_use_the_same_device(self):
        from app.services.ingestion_service import IngestionService
        from app.services.rag_service import RetrievalService
        from app.services.rerank_service import RerankService
        with patch("app.core.config.settings.local_model_device", "cpu"):
            self.assertEqual(IngestionService._resolve_embedding_device(), "cpu")
            self.assertEqual(RetrievalService._resolve_embedding_device(), "cpu")
            self.assertEqual(RerankService._resolve_device(), "cpu")


class ScalingAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_oversized_tracking_names_are_rejected_before_storage_or_database_access(self):
        from io import BytesIO
        from fastapi import HTTPException, UploadFile
        from starlette.datastructures import Headers
        from app.api.endpoints import ingest
        for course, filename in (("X" * 256, "fixture.pdf"), ("course", "x" * 252 + ".pdf"), ("ß" * 200, "fixture.pdf")):
            with self.subTest(course_length=len(course), filename_length=len(filename)):
                session = SimpleNamespace(execute=AsyncMock())
                upload = UploadFile(file=BytesIO(b"%PDF-invalid"), filename=filename, headers=Headers({"content-type": "application/pdf"}))
                with patch.object(ingest.storage_service, "put_pdf") as store:
                    with self.assertRaises(HTTPException) as caught:
                        await ingest.upload_document(course_id=course, file=upload, db=session)
                self.assertEqual(caught.exception.status_code, 400)
                session.execute.assert_not_awaited()
                store.assert_not_called()

    async def test_sql_usage_counts_unknown_records_conservatively(self):
        session = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(one=lambda: (2, 10, 30, 40, 1, 1))))
        usage = await ResourceBudgetService().usage(session, exclude_upload_id="own")
        sql = str(session.execute.call_args.args[0].compile(dialect=postgresql.dialect()))
        self.assertIn("coalesce", sql.lower())
        self.assertIn("source_bytes IS NULL", sql)
        self.assertIn("reserved_chunk_count IS NULL", sql)
        self.assertIn("document_uploads.upload_id !=", sql)
        self.assertEqual(usage.unknown_documents, 1)

    async def test_cursor_roundtrip_and_tie_breaking_pagination_query(self):
        row = SimpleNamespace(created_at=datetime.now(timezone.utc), upload_id="a")
        cursor = encode_upload_cursor(row)
        self.assertEqual(decode_upload_cursor(cursor), (row.created_at, "a"))
        session = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [row, row]))))
        records, next_cursor = await paginated_uploads(session, limit=1, cursor=cursor)
        query = str(session.execute.call_args.args[0].compile(dialect=postgresql.dialect()))
        self.assertIn("row_number() OVER", query)
        self.assertIn("(document_uploads.created_at, document_uploads.upload_id) <", query)
        self.assertEqual(records, [row])
        self.assertEqual(next_cursor, cursor)

    async def test_invalid_cursor_is_rejected_before_database_access(self):
        session = SimpleNamespace(execute=AsyncMock())
        for cursor in ("not-base64!", "x" * 513, "WzEsMl0", "WyIyMDI2LTAxLTAxIiwiYSJd"):
            with self.assertRaises(ValueError):
                await paginated_uploads(session, cursor=cursor)
        session.execute.assert_not_awaited()

    async def test_current_attempt_updates_take_a_row_lock(self):
        from app.services.upload_service import UploadService
        session = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: None)))
        await UploadService()._get_current_upload(session, "upload", "task", lease_owner="worker")
        sql = str(session.execute.call_args.args[0].compile(dialect=postgresql.dialect()))
        self.assertIn("FOR UPDATE", sql)
        self.assertIn("document_uploads.task_id", sql)
        self.assertIn("document_uploads.lease_owner", sql)

    async def test_provider_budget_rejection_sends_no_request_and_records_no_actual_attempt(self):
        from app.services import shared_provider_service as shared
        generate = Mock()
        with patch.object(shared, "reserve_provider_request", AsyncMock(side_effect=LLMProviderRateLimitError("budget"))), \
             patch.object(shared.provider_metrics, "observe") as observe:
            with self.assertRaises(LLMProviderRateLimitError):
                await shared.observe_provider_call("groq", "graph_extraction", generate, input_characters=50)
        generate.assert_not_called()
        observe.assert_not_called()

    async def test_split_process_mode_cannot_silently_skip_shared_budgets(self):
        from app.services import shared_provider_service as shared
        config = Settings(_env_file=None, PROCESSING_ROLE="worker", SHARED_PROVIDER_LIMITS_ENABLED=False)
        with patch.object(shared, "settings", config):
            with self.assertRaises(LLMProviderUnavailableError):
                await shared.reserve_provider_request("groq")

    async def test_cancelled_thread_operation_drains_before_lease_can_be_released(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        def write():
            entered.set()
            release.wait(timeout=2)
            finished.set()
        task = asyncio.create_task(run_blocking_safely(write))
        while not entered.is_set():
            await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(.01)
        task.cancel()
        await asyncio.sleep(.01)
        self.assertFalse(task.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(finished.is_set())

    async def test_application_budget_is_not_reported_as_vendor_quota(self):
        from app.core.exceptions import SharedProviderBudgetExceeded
        from app.services.ingestion_service import IngestionService
        from app.schemas.extraction import GraphBatchDiagnostic
        category = IngestionService._graph_failure_category(SharedProviderBudgetExceeded("application limit"))
        self.assertEqual(category, "application_budget")
        diagnostic = GraphBatchDiagnostic(section_label="Fixture", outcome="skipped", category=category)
        self.assertEqual(diagnostic.category, category)

    async def test_vector_cleanup_is_offloaded_and_completes_before_graph_cleanup(self):
        from app.services.ingestion_service import IngestionService
        service = IngestionService(SimpleNamespace(), SimpleNamespace())
        events = []
        service.cleanup_vectors = Mock(side_effect=lambda _: events.append(("vectors", threading.get_ident())))
        service.cleanup_graph = AsyncMock(side_effect=lambda *_: events.append(("graph", threading.get_ident())))
        await service.cleanup_upload("upload", "course")
        self.assertEqual([name for name, _ in events], ["vectors", "graph"])
        self.assertNotEqual(events[0][1], threading.get_ident())
        self.assertEqual(events[1][1], threading.get_ident())

    async def test_vector_cleanup_failure_does_not_continue_to_graph_cleanup(self):
        from app.services.ingestion_service import IngestionService
        service = IngestionService(SimpleNamespace(), SimpleNamespace())
        service.cleanup_vectors = Mock(side_effect=RuntimeError("unavailable"))
        service.cleanup_graph = AsyncMock()
        with self.assertRaises(RuntimeError):
            await service.cleanup_upload("upload", "course")
        service.cleanup_graph.assert_not_awaited()

    async def test_worker_role_requires_correct_entrypoint_and_shared_limits(self):
        from app import worker
        with patch.object(worker, "settings", Settings(_env_file=None)), \
             patch.object(worker, "initialize_database_schema", AsyncMock()) as initialize:
            with self.assertRaises(RuntimeError):
                await worker.run_worker()
            initialize.assert_not_awaited()

    async def test_api_role_reports_offline_worker_without_disabling_graph_queries(self):
        import app.main as main
        from app.services import worker_presence_service as presence
        session = AsyncMock()
        config = Settings(_env_file=None, PROCESSING_ROLE="api", SHARED_PROVIDER_LIMITS_ENABLED=True)
        with patch.object(main, "settings", config), \
             patch.object(main, "postgres_engine", SimpleNamespace(connect=Mock(return_value=session))), \
             patch.object(main.qdrant_client, "get_collections", return_value=[]), \
             patch.object(main.neo4j_driver, "verify_connectivity", AsyncMock()), \
             patch.object(main.storage_service, "check_ready"), \
             patch.object(presence, "active_worker_count", AsyncMock(return_value=0)):
            ready = await main.readiness_check()
        self.assertTrue(ready["graph_available"])
        self.assertFalse(ready["ingestion_available"])
        self.assertEqual(ready["degraded_services"], ["ingestion_worker"])


class InventoryRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def run_refresh(self, *, apply, current_status="ready", missing_source=False):
        from scripts.refresh_resource_inventory import refresh
        from app.core import database
        from app.services.resource_budget_service import resource_budget_service
        from app.services.storage_service import storage_service
        record = SimpleNamespace(upload_id="fixture", storage_key="fixture.pdf")
        current = SimpleNamespace(status=current_status, storage_key="fixture.pdf", page_count=None,
                                  source_bytes=None, reserved_chunk_count=None, processed_chunk_count=3)
        read_session = AsyncMock()
        read_session.execute.return_value = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [record]))
        write_session = AsyncMock()
        write_session.execute.return_value = SimpleNamespace(scalar_one_or_none=lambda: current)
        read_context, write_context = AsyncMock(), AsyncMock()
        read_context.__aenter__.return_value = read_session
        write_context.__aenter__.return_value = write_session
        pdf_context = Mock()
        pdf_context.__enter__ = Mock(return_value=SimpleNamespace(needs_pass=False, page_count=2))
        pdf_context.__exit__ = Mock(return_value=False)
        with patch.object(database, "AsyncSessionLocal", side_effect=[read_context, write_context]) as sessions, \
             patch.object(database, "close_database_connections", AsyncMock()) as close, \
             patch("scripts.evaluate_ingestion_scaling.require_local_database"), \
             patch.object(storage_service, "get_bytes", side_effect=FileNotFoundError() if missing_source else None,
                          return_value=b"pdf"), \
             patch("scripts.refresh_resource_inventory.fitz.open", return_value=pdf_context), \
             patch.object(resource_budget_service, "lock", AsyncMock()) as lock:
            result = await refresh(apply=apply)
        close.assert_awaited_once()
        return result, current, write_session, sessions, lock

    async def test_inventory_dry_run_does_not_open_a_write_transaction(self):
        result, _, write_session, sessions, lock = await self.run_refresh(apply=False)
        self.assertEqual(result, {"inspected": 1, "updated": 0, "skipped_or_unavailable": 0, "dry_run": True})
        self.assertEqual(sessions.call_count, 1)
        write_session.commit.assert_not_awaited()
        lock.assert_not_awaited()

    async def test_inventory_apply_locks_and_updates_only_source_metadata(self):
        result, current, write_session, _, lock = await self.run_refresh(apply=True)
        self.assertEqual(result["updated"], 1)
        self.assertEqual((current.page_count, current.source_bytes, current.reserved_chunk_count), (2, 3, 3))
        lock.assert_awaited_once_with(write_session)
        write_session.commit.assert_awaited_once()
        query = str(write_session.execute.call_args.args[0].compile(dialect=postgresql.dialect()))
        self.assertIn("FOR UPDATE", query)

    async def test_inventory_apply_skips_a_document_that_became_active(self):
        result, current, write_session, _, _ = await self.run_refresh(apply=True, current_status="processing")
        self.assertEqual(result["skipped_or_unavailable"], 1)
        self.assertIsNone(current.page_count)
        write_session.commit.assert_not_awaited()

    async def test_inventory_missing_source_is_counted_without_mutating_metadata(self):
        result, _, write_session, sessions, _ = await self.run_refresh(apply=True, missing_source=True)
        self.assertEqual((result["inspected"], result["skipped_or_unavailable"]), (0, 1))
        self.assertEqual(sessions.call_count, 1)
        write_session.commit.assert_not_awaited()


class GraphDisplayTests(unittest.TestCase):
    def test_bounded_subset_has_no_dangling_edges_and_does_not_mutate_inputs(self):
        nodes = [{"id": str(index), "name": f"Concept {index}"} for index in range(10)]
        edges = [{"source": "0", "target": str(index), "type": "PART_OF"} for index in range(1, 10)]
        context = [{"concept": nodes[0], "related_concepts": nodes[1:], "relationships": edges}]
        result, limited, node_count, edge_count = bound_graph_context(context, max_nodes=5, max_edges=2)
        self.assertTrue(limited)
        self.assertEqual((node_count, edge_count), (5, 2))
        ids = {node["id"] for node in [result[0]["concept"], *result[0]["related_concepts"]]}
        self.assertTrue(all(edge["source"] in ids and edge["target"] in ids for edge in result[0]["relationships"]))
        self.assertEqual(len(context[0]["related_concepts"]), 9)

    def test_query_anchors_are_retained_before_neighbors(self):
        context = [{"concept": {"id": "anchor1"}, "related_concepts": [{"id": "neighbor"}]},
                   {"concept": {"id": "anchor2"}, "related_concepts": []}]
        result, _, _, _ = bound_graph_context(context, max_nodes=2, max_edges=2)
        self.assertEqual([item["concept"]["id"] for item in result], ["anchor1", "anchor2"])
        self.assertEqual(result[0]["related_concepts"], [])
