import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.core.exceptions import LLMProviderUnavailableError
from app.schemas.extraction import ConceptNode, ConceptRelationship, GraphExtractionResponse
from app.services.ingestion_service import IngestionService
from app.services.parser_service import DocumentChunk


def graph(names, *, connected=True, chunk="chunk"):
    return GraphExtractionResponse(
        nodes=[ConceptNode(id=str(i), name=name, type="topic", source_chunk_id=chunk)
               for i, name in enumerate(names)],
        relationships=[ConceptRelationship(source_node_id="0", target_node_id="1",
                                            relation_type="PREREQUISITE_OF")]
        if connected and len(names) > 1 else [],
    )


class GraphPipelineRegressions(unittest.TestCase):
    def service(self):
        return IngestionService(graph_driver=SimpleNamespace(), vector_client=SimpleNamespace())

    def chunks(self, count=1):
        return [DocumentChunk(id=f"chunk-{i}", text="HTTP is a prerequisite for HTTPS.",
                              metadata={"section_heading": f"Section {i}", "page_number": i + 1})
                for i in range(count)]

    def merge(self, parts):
        return IngestionService._merge_graph_extractions(
            parts, sections_total=2, sections_succeeded=2, sections_failed=0,
            batches_total=2, batches_succeeded=2, batches_failed=0,
            batches_skipped=0, provider_limited=False, extraction_budget_applied=False,
            failed_section_labels=[],
        )

    def test_response_local_ids_do_not_collide_across_sections(self):
        merged = self.merge([graph(["HTTP", "HTTPS"]), graph(["TCP", "TLS"])])
        self.assertEqual(len(merged.nodes), 4)
        self.assertEqual(len({node.id for node in merged.nodes}), 4)
        by_id = {node.id: node.name for node in merged.nodes}
        self.assertEqual({(by_id[r.source_node_id], by_id[r.target_node_id])
                          for r in merged.relationships}, {("HTTP", "HTTPS"), ("TCP", "TLS")})
        self.assertEqual(merged.model_dump(), self.merge([graph(["HTTP", "HTTPS"]),
                                                        graph(["TCP", "TLS"])]).model_dump())

    def test_names_still_merge_when_provider_ids_are_reused(self):
        merged = self.merge([graph(["HTTP", "HTTPS"]), graph(["TCP", "http"])])
        self.assertEqual(len(merged.nodes), 3)
        self.assertEqual(len(merged.relationships), 2)

    def test_single_chunk_can_recover_missing_relationship(self):
        service = self.service()
        service.extract_graph_from_text = AsyncMock(side_effect=[
            graph(["HTTP", "HTTPS"], connected=False, chunk="chunk-0"),
            graph(["HTTP", "HTTPS"], chunk="chunk-0"),
        ])
        with patch("app.services.ingestion_service.settings.graph_global_linking_enabled", True):
            result = asyncio.run(service.extract_graph_from_chunks(self.chunks()))
        self.assertTrue(result.global_linking_succeeded)
        self.assertEqual(len(result.relationships), 1)
        self.assertIn("both nodes and relationships", service.extract_graph_from_text.await_args.args[0])

    def test_recovery_without_new_edges_is_not_reported_as_success(self):
        service = self.service()
        service.extract_graph_from_text = AsyncMock(return_value=
            graph(["HTTP", "HTTPS"], connected=False, chunk="chunk-0"))
        with patch("app.services.ingestion_service.settings.graph_global_linking_enabled", True):
            result = asyncio.run(service.extract_graph_from_chunks(self.chunks()))
        self.assertTrue(result.global_linking_attempted)
        self.assertFalse(result.global_linking_succeeded)

    def test_timeout_skips_are_not_labelled_as_demo_budget(self):
        service = self.service()
        service.extract_graph_from_text = AsyncMock(side_effect=LLMProviderUnavailableError("timeout"))
        with patch("app.services.ingestion_service.settings.graph_max_batches", 10):
            result = asyncio.run(service.extract_graph_from_chunks(self.chunks(3)))
        self.assertEqual(result.batches_skipped, 1)
        self.assertEqual(result.batches_failed, 2)
        self.assertFalse(result.extraction_budget_applied)

    def test_real_budget_is_still_reported(self):
        service = self.service()
        service.extract_graph_from_text = AsyncMock(return_value=graph(["HTTP"], chunk="chunk-0"))
        with patch("app.services.ingestion_service.settings.graph_max_batches", 1):
            result = asyncio.run(service.extract_graph_from_chunks(self.chunks(3)))
        self.assertTrue(result.extraction_budget_applied)
        self.assertEqual(result.batches_skipped, 2)
