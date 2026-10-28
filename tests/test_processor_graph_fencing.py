"""Opt-in processor contracts; no database/provider calls in these tests."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from app.core.config import Settings
from app.services import document_processing_service as processing
from app.services.ingestion_service import IngestionService


class ProcessorGraphFencingTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self):
        from tests.test_processing import DocumentProcessingServiceTests
        fixture = DocumentProcessingServiceTests("test_shared_processor_preserves_all_durable_stage_transitions")
        service, uploads, ingestion, chunk = fixture._service_fixture()
        self.assertTrue(hasattr(service.config, "graph_projection_fencing_enabled"), "Projection rollout gate is missing")
        service.config.graph_projection_fencing_enabled = True
        uploads.get_upload.return_value.attempt_count = 2
        uploads._get_current_upload = AsyncMock(return_value=uploads.get_upload.return_value)
        ingestion.replace_graph_extraction = AsyncMock()
        ingestion.cleanup_graph_attempt = AsyncMock()
        @asynccontextmanager
        async def sessions():
            yield SimpleNamespace()
        return service, uploads, ingestion, sessions

    async def run_processor(self, service, sessions):
        with (
            patch.object(processing, "AsyncSessionLocal", sessions),
            patch.object(processing.settings, "embedding_provider", "local"),
            patch.object(processing.storage_service, "get_bytes", return_value=b"%PDF-fixture"),
            patch.object(processing.storage_service, "exists", return_value=True),
        ):
            return await service.process_document("upload-1", "task-1", lease_owner="instance-1")

    async def test_projection_integration_is_disabled_by_default(self):
        self.assertIs(getattr(Settings(_env_file=None), "graph_projection_fencing_enabled", None), False)

    async def test_opt_in_processor_replaces_atomically_using_locked_source_generation(self):
        service, uploads, ingestion, sessions = self.fixture()
        result = await self.run_processor(service, sessions)
        self.assertEqual(result["status"], "ready")
        ingestion.cleanup_graph.assert_not_awaited()
        ingestion.store_graph_extraction.assert_not_awaited()
        ingestion.replace_graph_extraction.assert_awaited_once()
        self.assertEqual(ingestion.replace_graph_extraction.await_args.kwargs["source_generation"], 2)
        self.assertEqual(ingestion.replace_graph_extraction.await_args.kwargs["execution_token"], "task-1")
        uploads._get_current_upload.assert_awaited()

    async def test_stale_source_cannot_enter_any_remote_write(self):
        service, uploads, ingestion, sessions = self.fixture()
        uploads._get_current_upload.return_value = None
        result = await self.run_processor(service, sessions)
        self.assertEqual(result["status"], "superseded")
        ingestion.cleanup_vectors.assert_not_called()
        ingestion.replace_graph_extraction.assert_not_awaited()
        ingestion.cleanup_upload.assert_not_awaited()
        uploads.mark_failed.assert_not_awaited()

    async def test_opt_in_failure_closes_attempt_then_cleans_only_its_vectors(self):
        service, uploads, ingestion, sessions = self.fixture()
        ingestion.upsert_chunks_to_qdrant.side_effect = RuntimeError("synthetic indexing failure")
        result = await self.run_processor(service, sessions)
        self.assertEqual(result["status"], "failed")
        ingestion.cleanup_upload.assert_not_awaited()
        ingestion.cleanup_graph_attempt.assert_awaited_once_with("upload-1", "course-1",
            execution_token="task-1", source_generation=2)
        self.assertEqual(ingestion.cleanup_vectors.call_args.kwargs, {"execution_token": "task-1"})
        uploads.mark_failed.assert_awaited_once()

    async def test_permanent_cleanup_uses_tombstone_instead_of_raw_graph_deletion(self):
        service = IngestionService(graph_driver=SimpleNamespace(), vector_client=SimpleNamespace())
        service.cleanup_vectors = Mock()
        service.cleanup_graph = AsyncMock()
        service.tombstone_graph_publication = AsyncMock()
        await service.cleanup_upload("upload", "course", permanent=True)
        service.cleanup_vectors.assert_called_once_with("upload")
        service.tombstone_graph_publication.assert_awaited_once_with("upload", "course")
        service.cleanup_graph.assert_not_awaited()

    async def test_attempt_vector_cleanup_filters_upload_and_execution_token(self):
        vector = SimpleNamespace(collection_exists=Mock(return_value=True), delete=Mock())
        service = IngestionService(graph_driver=SimpleNamespace(), vector_client=vector)
        service.cleanup_vectors("upload", execution_token="task")
        conditions = vector.delete.call_args.kwargs["points_selector"].filter.must
        self.assertEqual({item.key: item.match.value for item in conditions},
            {"upload_id": "upload", "execution_token": "task"})
