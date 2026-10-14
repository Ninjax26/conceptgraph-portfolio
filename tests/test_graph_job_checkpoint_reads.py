"""Source/configuration-qualified reads; no provider or database requests."""

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from app.schemas.extraction import ConceptNode, GraphExtractionResponse
from app.services.graph_continuation_service import GraphContinuationService, validated_job_checkpoints
from app.services.ingestion_service import IngestionService
from app.services.parser_service import DocumentChunk


class QualifiedCheckpointReads(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch("app.services.ingestion_service.settings.graph_global_linking_enabled", True))
        self.chunks = [DocumentChunk(id=f"chunk-{i}", text=f"DNS evidence {i}", metadata={
            "upload_id":"fixture", "document_name":"fixture.pdf", "page_number":1,
            "section_heading":"DNS"}) for i in range(2)]
        self.batch = IngestionService._graph_section_batches(self.chunks)[0]
        self.graph = GraphExtractionResponse(nodes=[ConceptNode(id="dns", name="DNS", type="concept", source_chunk_id="chunk-0"),
            ConceptNode(id="ip", name="IP address", type="concept", source_chunk_id="chunk-1")])
        self.payloads = {self.batch.batch_key:self.graph.model_dump(mode="json")}

    def test_rebinds_current_sources_without_mutating_saved_json(self):
        result = validated_job_checkpoints(self.chunks, self.payloads)
        self.assertEqual(result[self.batch.batch_key].nodes[0].upload_id, "fixture")
        self.assertEqual(result[self.batch.batch_key].nodes[0].document_name, "fixture.pdf")
        self.assertEqual(self.payloads[self.batch.batch_key]["nodes"][0]["upload_id"], "")

    def test_reuses_explicit_empty_recovery_not_unversioned_rows(self):
        recovery = IngestionService.recovery_checkpoint_batch(self.chunks, {self.batch.batch_key:self.graph})
        self.payloads[recovery.batch_key] = GraphExtractionResponse(empty_extraction=True).model_dump(mode="json")
        self.payloads["legacy"] = self.graph.model_dump(mode="json")
        result = validated_job_checkpoints(self.chunks, self.payloads)
        self.assertEqual(set(result), {self.batch.batch_key, recovery.batch_key})
        self.assertTrue(result[recovery.batch_key].empty_extraction)

    def test_changed_concepts_invalidate_old_recovery(self):
        recovery = IngestionService.recovery_checkpoint_batch(self.chunks, {self.batch.batch_key:self.graph})
        self.payloads[recovery.batch_key] = GraphExtractionResponse(empty_extraction=True).model_dump(mode="json")
        self.payloads[self.batch.batch_key]["nodes"][0]["name"] = "Domain Name System"
        self.assertNotIn(recovery.batch_key, validated_job_checkpoints(self.chunks, self.payloads))

    def test_corrupted_or_foreign_or_limited_sections_are_not_reused(self):
        for payload in (None, {"nodes":[]}, {**self.graph.model_dump(mode="json"), "provider_limited":True}):
            with self.subTest(payload=payload):
                self.assertEqual(validated_job_checkpoints(self.chunks, {self.batch.batch_key:payload}), {})
        self.payloads[self.batch.batch_key]["nodes"][0]["source_chunk_id"] = "foreign"
        self.assertEqual(validated_job_checkpoints(self.chunks, self.payloads), {})

    def test_changed_model_route_does_not_reuse_older_section(self):
        with patch("app.services.ingestion_service.settings.groq_model", "different-model"):
            self.assertEqual(validated_job_checkpoints(self.chunks, self.payloads), {})


class ReadTransactionBoundary(unittest.IsolatedAsyncioTestCase):
    async def test_lost_claim_returns_none_without_reading_checkpoints(self):
        service = GraphContinuationService()
        session = SimpleNamespace(rollback=AsyncMock(), execute=AsyncMock())
        with patch.object(service, "_locked_claim", AsyncMock(return_value=None)):
            self.assertIsNone(await service.load_checkpoints(session, SimpleNamespace()))
        session.execute.assert_not_awaited()
        session.rollback.assert_awaited()

    async def test_snapshot_failure_releases_read_locks(self):
        service = GraphContinuationService()
        session = SimpleNamespace(rollback=AsyncMock(), execute=AsyncMock())
        with patch.object(service, "_locked_claim", AsyncMock(return_value=(SimpleNamespace(), SimpleNamespace(snapshot_json={}), None))), \
             patch("app.services.graph_continuation_service.snapshot_chunks", side_effect=ValueError("bad snapshot")):
            with self.assertRaises(ValueError):
                await service.load_checkpoints(session, SimpleNamespace())
        session.execute.assert_not_awaited()
        session.rollback.assert_awaited_once()
