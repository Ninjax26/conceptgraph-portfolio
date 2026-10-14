"""Authored graph fixtures: recovery identity, not semantic relationship accuracy."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from app.schemas.extraction import ConceptNode, ConceptRelationship, GraphExtractionResponse
from app.services.ingestion_service import IngestionService
from app.services.parser_service import DocumentChunk


class RecoveryCheckpointIdentityTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch("app.services.ingestion_service.settings.graph_global_linking_enabled", True))
        self.chunks = [DocumentChunk(id=f"chunk-{index}", text=f"DNS evidence {index}.", metadata={
            "upload_id":"fixture", "document_name":"fixture.pdf", "page_number":1, "section_heading":"DNS"
        }) for index in range(2)]
        self.batch = IngestionService._graph_section_batches(self.chunks)[0]
        self.graph = GraphExtractionResponse(nodes=[ConceptNode(id="dns", name="DNS", type="concept", source_chunk_id="chunk-0"),
            ConceptNode(id="ip", name="IP address", type="concept", source_chunk_id="chunk-1")])
        self.saved = {self.batch.batch_key:self.graph}

    def test_reconstructed_identity_matches_actual_extraction_callback(self):
        service = IngestionService(SimpleNamespace(), SimpleNamespace())
        service.extract_graph_from_text = AsyncMock(return_value=GraphExtractionResponse())
        callback = AsyncMock()
        asyncio.run(service.extract_graph_from_chunks(self.chunks, completed_batches=self.saved, on_batch_completed=callback))
        expected = IngestionService.recovery_checkpoint_batch(self.chunks, self.saved)
        self.assertEqual(callback.call_args.args[0], expected)
        self.assertTrue(expected.batch_key.startswith("global:v5:"))

    def test_saved_global_rows_do_not_authorize_themselves(self):
        expected = IngestionService.recovery_checkpoint_batch(self.chunks, self.saved)
        with_global = {**self.saved, expected.batch_key:self.graph, "section:legacy":self.graph}
        self.assertEqual(expected, IngestionService.recovery_checkpoint_batch(self.chunks, with_global))
        self.assertIsNone(IngestionService.recovery_checkpoint_batch(self.chunks, {expected.batch_key:self.graph}))

    def test_changed_preceding_concepts_change_recovery_identity(self):
        expected = IngestionService.recovery_checkpoint_batch(self.chunks, self.saved)
        changed = self.graph.model_copy(update={"nodes":[self.graph.nodes[0].model_copy(update={"name":"Domain Name System"}), self.graph.nodes[1]]})
        rebuilt = IngestionService.recovery_checkpoint_batch(self.chunks, {self.batch.batch_key:changed})
        self.assertNotEqual(expected.batch_key, rebuilt.batch_key)

    def test_invalid_or_foreign_sources_cannot_authorize_recovery(self):
        foreign = self.graph.model_copy(update={"nodes":[self.graph.nodes[0].model_copy(update={"source_chunk_id":"foreign"}), self.graph.nodes[1]]})
        self.assertIsNone(IngestionService.recovery_checkpoint_batch(self.chunks, {self.batch.batch_key:foreign}))
        invalid = self.graph.model_copy(update={"relationships":[ConceptRelationship(source_node_id="dns", target_node_id="missing", relation_type="PART_OF")]})
        self.assertIsNone(IngestionService.recovery_checkpoint_batch(self.chunks, {self.batch.batch_key:invalid}))

    def test_complete_single_section_graph_does_not_require_recovery(self):
        complete = self.graph.model_copy(update={"relationships":[ConceptRelationship(source_node_id="dns", target_node_id="ip", relation_type="RELATED_TO")]})
        self.assertIsNone(IngestionService.recovery_checkpoint_batch(self.chunks, {self.batch.batch_key:complete}))

    def test_global_disabled_has_no_recovery_identity(self):
        with patch("app.services.ingestion_service.settings.graph_global_linking_enabled", False):
            self.assertIsNone(IngestionService.recovery_checkpoint_batch(self.chunks, self.saved))

    def test_section_order_is_independent_of_checkpoint_database_order(self):
        chunks = [replace(chunk, metadata={**chunk.metadata, "section_heading":f"Section {index}"}) for index, chunk in enumerate(self.chunks)]
        batches = IngestionService._graph_section_batches(chunks)
        saved = {batch.batch_key:GraphExtractionResponse(nodes=[self.graph.nodes[index]]) for index, batch in enumerate(batches)}
        expected = IngestionService.recovery_checkpoint_batch(chunks, saved)
        self.assertEqual(expected, IngestionService.recovery_checkpoint_batch(chunks, dict(reversed(list(saved.items())))))
