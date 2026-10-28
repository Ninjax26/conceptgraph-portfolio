"""Graph refreshes and initial ingestion must expose the same graph counters."""

import unittest

from app.schemas.extraction import ConceptNode, ConceptRelationship, GraphExtractionResponse
from app.services.graph_result_metadata import graph_result_metadata


class GraphResultMetadataTests(unittest.TestCase):
    def test_empty_output_preserves_existing_result_schema(self):
        self.assertEqual(graph_result_metadata(GraphExtractionResponse()), {
            "sections_scanned_locally": 0,
            "nodes_upserted": 0, "relationships_upserted": 0,
            "graph_status": "READY_WITHOUT_GRAPH",
            "graph_sections_total": 0, "graph_sections_succeeded": 0,
            "graph_sections_failed": 0, "graph_sections_processed": 0,
            "graph_sections_empty": 0, "graph_batches_empty": 0,
            "graph_batches_total": 0, "graph_batches_succeeded": 0,
            "graph_batches_failed": 0, "graph_batches_skipped": 0,
            "graph_provider_limited": False, "graph_extraction_budget_applied": False,
            "graph_global_linking_attempted": False, "graph_global_linking_succeeded": False,
            "graph_global_linking_processed": False, "graph_relationship_pass_version": 0,
            "graph_checkpointed_batches": 0, "graph_failed_sections": [],
            "graph_batch_diagnostics": [],
        })

    def test_incomplete_coverage_stays_partial_even_with_connected_concepts(self):
        graph = GraphExtractionResponse(nodes=[ConceptNode(id="a", name="DNS", type="concept"),
            ConceptNode(id="b", name="IP", type="concept")], relationships=[
                ConceptRelationship(source_node_id="a", target_node_id="b", relation_type="RELATED_TO")],
            sections_total=3, sections_succeeded=1, sections_empty=1,
            batches_succeeded=2, global_linking_attempted=True)
        result = graph_result_metadata(graph)
        self.assertEqual(result["graph_status"], "GRAPH_PARTIAL")
        self.assertEqual(result["nodes_upserted"], 2)
        self.assertEqual(result["relationships_upserted"], 1)
        self.assertEqual(result["graph_checkpointed_batches"], 2)
        self.assertEqual(result["graph_relationship_pass_version"], 4)
        graph.sections_succeeded = 2
        self.assertEqual(graph_result_metadata(graph)["graph_status"], "GRAPH_READY")
