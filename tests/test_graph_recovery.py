"""Graph recovery contracts: bounded requests, source integrity, and durable outcomes."""

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx

from app.core.exceptions import GraphFailureReason, GraphStructureError, LLMProviderRateLimitError
from app.core.processing import GraphStatus, assess_graph_status
from app.schemas.extraction import ConceptNode, GraphExtractionResponse
from app.services.ingestion_service import IngestionService
from app.services.parser_service import DocumentChunk
from app.services.provider_failover import provider_circuit_breaker
from app.services.provider_metrics import ProviderMetrics
from app.services.gemini_service import GeminiService
from app.services.upload_service import UploadService


class GraphRecoveryTests(unittest.TestCase):
    def setUp(self):
        provider_circuit_breaker.clear()
        self.addCleanup(provider_circuit_breaker.clear)
        for key, value in (("groq_api_key", "test"), ("gemini_api_key", None),
                           ("cerebras_api_key", None), ("llm_provider", "groq"),
                           ("graph_global_linking_enabled", False),
                           ("graph_json_repair_enabled", True)):
            self.enterContext(patch(f"app.core.config.settings.{key}", value))
        self.service = IngestionService(SimpleNamespace(), SimpleNamespace())

    @staticmethod
    def chunk(index=0):
        return DocumentChunk(id=f"chunk-{index}", text="Evidence supports this concept.",
                             metadata={"section_heading": f"Section {index}",
                                       "page_number": index + 1, "upload_id": "upload"})

    @staticmethod
    def graph(index=0):
        return GraphExtractionResponse(nodes=[ConceptNode(
            id=f"n-{index}", name=f"Concept {index}", type="concept",
            source_chunk_id=f"chunk-{index}")])

    def test_explicit_empty_graph_is_checkpointed_and_not_requested_again(self):
        chunk = self.chunk()
        checkpoint = AsyncMock()
        self.service.extract_graph_from_text = AsyncMock(return_value=GraphExtractionResponse())
        graph = asyncio.run(self.service.extract_graph_from_chunks(
            [chunk], on_batch_completed=checkpoint))
        self.assertEqual(graph.batches_failed, 0)
        self.assertEqual(graph.sections_failed, 0)
        self.assertEqual(graph.sections_empty, 1)
        self.assertEqual(graph.sections_processed, 1)
        batch, accepted = checkpoint.call_args.args
        self.service.extract_graph_from_text.reset_mock()
        resumed = asyncio.run(self.service.extract_graph_from_chunks(
            [chunk], completed_batches={batch.batch_key: accepted}))
        self.service.extract_graph_from_text.assert_not_awaited()
        self.assertEqual(resumed.sections_empty, 1)

    def test_failure_diagnostics_do_not_copy_provider_error_text(self):
        self.service.extract_graph_from_text = AsyncMock(
            side_effect=GraphStructureError("private prompt and key must not appear"))
        graph = asyncio.run(self.service.extract_graph_from_chunks([self.chunk()]))
        self.assertEqual(graph.batches_failed, 1)
        self.assertEqual(graph.batch_diagnostics[0].category, "invalid_schema")
        self.assertNotIn("private prompt", graph.model_dump_json())

    def test_invalid_source_output_is_not_saved_as_an_empty_success(self):
        bad = self.graph().model_copy(update={"nodes": [ConceptNode(
            id="bad", name="Bad", type="concept", source_chunk_id="invented")]})
        self.service.extract_graph_from_text = AsyncMock(return_value=bad)
        checkpoint = AsyncMock()
        graph = asyncio.run(self.service.extract_graph_from_chunks(
            [self.chunk()], on_batch_completed=checkpoint))
        self.assertEqual(graph.batches_failed, 1)
        self.assertEqual(graph.sections_empty, 0)
        checkpoint.assert_not_awaited()
        self.assertEqual(graph.batch_diagnostics[0].category, "unknown_source")

    def test_resume_requests_only_the_failed_batch(self):
        chunks = [self.chunk(0), self.chunk(1)]
        checkpoint = AsyncMock()
        self.service.extract_graph_from_text = AsyncMock(
            side_effect=[self.graph(0), GraphStructureError("invalid")])
        first = asyncio.run(self.service.extract_graph_from_chunks(
            chunks, on_batch_completed=checkpoint))
        self.assertEqual(len(first.nodes), 1)
        batch, accepted = checkpoint.call_args.args
        self.service.extract_graph_from_text = AsyncMock(return_value=self.graph(1))
        resumed = asyncio.run(self.service.extract_graph_from_chunks(
            chunks, completed_batches={batch.batch_key: accepted}))
        self.assertEqual(len(resumed.nodes), 2)
        self.service.extract_graph_from_text.assert_awaited_once()
        self.assertEqual(resumed.batches_failed, 0)
        self.assertEqual(resumed.batch_diagnostics, [])

    def test_json_repair_budget_is_shared_across_failover(self):
        with patch("app.core.config.settings.gemini_api_key", "test"), \
             patch.object(self.service, "_extract_with_groq", side_effect=GraphStructureError("invalid")) as groq, \
             patch.object(self.service, "_extract_with_gemini", side_effect=GraphStructureError("invalid")) as gemini:
            with self.assertRaises(GraphStructureError):
                asyncio.run(self.service.extract_graph_from_text("evidence"))
        self.assertEqual(groq.call_count + gemini.call_count, 3)

    def test_repair_never_runs_after_backup_quota_failure(self):
        with patch("app.core.config.settings.gemini_api_key", "test"), \
             patch.object(self.service, "_extract_with_groq", side_effect=GraphStructureError("invalid")) as groq, \
             patch.object(self.service, "_extract_with_gemini", side_effect=LLMProviderRateLimitError("quota")) as gemini:
            with self.assertRaises(LLMProviderRateLimitError):
                asyncio.run(self.service.extract_graph_from_text("evidence"))
        self.assertEqual(groq.call_count + gemini.call_count, 2)

    def test_gemini_repair_is_validated_against_original_source_ids(self):
        with patch("app.core.config.settings.llm_provider", "gemini"), \
             patch("app.core.config.settings.gemini_api_key", "test"), \
             patch.object(self.service, "_extract_with_gemini", side_effect=[
                 GraphStructureError("invalid"), self.graph()]) as gemini:
            result = asyncio.run(self.service.extract_graph_from_text(
                "[Source chunk ID: chunk-0 | Page: 1]\nEvidence"))
        self.assertEqual(result.nodes[0].source_chunk_id, "chunk-0")
        self.assertEqual(gemini.call_count, 2)

    def test_missing_arrays_are_invalid_not_empty_graphs(self):
        with self.assertRaises(GraphStructureError) as caught:
            self.service._parse_provider_graph("{}")
        self.assertEqual(caught.exception.reason, "invalid_schema")

    def test_malformed_json_has_a_safe_specific_category(self):
        with self.assertRaises(GraphStructureError) as caught:
            self.service._parse_provider_graph("not JSON: confidential")
        self.assertEqual(caught.exception.reason, "malformed_json")
        self.assertNotIn("confidential", str(caught.exception))

    def test_unknown_relationship_type_cannot_pass_provider_validation(self):
        payload = {"nodes": [{"id": x, "name": x, "type": "concept"} for x in ("a", "b")],
                   "relationships": [{"source_node_id": "a", "target_node_id": "b",
                                      "relation_type": "IMAGINARY"}]}
        with self.assertRaises(GraphStructureError) as caught:
            self.service._parse_provider_graph(json.dumps(payload))
        self.assertEqual(caught.exception.reason, "unsupported_relationship_type")

    def test_structural_failures_have_specific_safe_reasons(self):
        node = {"id": "a", "name": "A", "type": "concept"}
        cases = [
            ({"nodes": [node, node], "relationships": []}, "duplicate_node_ids"),
            ({"nodes": [node], "relationships": [{"source_node_id": "a",
              "target_node_id": "missing", "relation_type": "PART_OF"}]}, "invalid_endpoints"),
            ({"nodes": [node] * 9, "relationships": []}, "batch_limit"),
            ({"nodes": "not an array", "relationships": []}, "invalid_schema"),
        ]
        for payload, category in cases:
            with self.subTest(category=category), self.assertRaises(GraphStructureError) as caught:
                self.service._parse_provider_graph(json.dumps(payload))
            self.assertEqual(caught.exception.reason, category)

    def test_legacy_empty_checkpoint_is_revalidated_not_assumed_successful(self):
        chunks = [self.chunk()]
        batch = self.service._graph_section_batches(chunks, batch_size=4)[0]
        self.service.extract_graph_from_text = AsyncMock(return_value=self.graph())
        graph = asyncio.run(self.service.extract_graph_from_chunks(
            chunks, completed_batches={batch.batch_key: GraphExtractionResponse()}))
        self.service.extract_graph_from_text.assert_awaited_once()
        self.assertEqual(graph.sections_succeeded, 1)

    def test_one_failed_batch_keeps_its_section_incomplete(self):
        chunks = [self.chunk(index) for index in range(2)]
        for chunk in chunks:
            chunk.metadata["section_heading"] = "Same section"
        self.service.extract_graph_from_text = AsyncMock(
            side_effect=[self.graph(0), GraphStructureError("invalid")])
        with patch("app.core.config.settings.graph_batch_size", 1):
            graph = asyncio.run(self.service.extract_graph_from_chunks(chunks))
        self.assertEqual(graph.sections_succeeded, 1)
        self.assertEqual(graph.sections_processed, 0)
        self.assertEqual(graph.sections_failed, 1)
        self.assertEqual(graph.failed_section_labels, ["Same section"])

    def test_filtered_source_nodes_are_reported_and_preserved_in_checkpoint(self):
        graph = self.graph()
        graph.nodes.append(ConceptNode(id="bad", name="Bad", type="concept", source_chunk_id="invented"))
        self.service.extract_graph_from_text = AsyncMock(return_value=graph)
        checkpoint = AsyncMock()
        accepted = asyncio.run(self.service.extract_graph_from_chunks(
            [self.chunk()], on_batch_completed=checkpoint))
        self.assertEqual(len(accepted.nodes), 1)
        self.assertEqual(accepted.batch_diagnostics[0].discarded_nodes, 1)
        batch, saved = checkpoint.call_args.args
        self.service.extract_graph_from_text.reset_mock()
        resumed = asyncio.run(self.service.extract_graph_from_chunks(
            [self.chunk()], completed_batches={batch.batch_key: saved}))
        self.service.extract_graph_from_text.assert_not_awaited()
        self.assertEqual(resumed.batch_diagnostics[0].discarded_nodes, 1)

    def test_graph_quality_does_not_penalize_a_processed_empty_section(self):
        self.assertEqual(assess_graph_status(
            3, 1, sections_total=2, sections_succeeded=1, sections_empty=1,
        ), GraphStatus.GRAPH_READY)
        self.assertEqual(assess_graph_status(
            0, 0, sections_total=1, sections_succeeded=0, sections_empty=1,
        ), GraphStatus.READY_WITHOUT_GRAPH)

    def test_empty_global_linking_result_is_checkpointed_without_noop_retries(self):
        chunks = [self.chunk(0), self.chunk(1)]
        self.service.extract_graph_from_text = AsyncMock(
            side_effect=[self.graph(0), self.graph(1), GraphExtractionResponse()])
        checkpoint = AsyncMock()
        with patch("app.core.config.settings.graph_global_linking_enabled", True):
            graph = asyncio.run(self.service.extract_graph_from_chunks(
                chunks, on_batch_completed=checkpoint))
            cached = {call.args[0].batch_key: call.args[1] for call in checkpoint.call_args_list}
            self.service.extract_graph_from_text.reset_mock()
            resumed = asyncio.run(self.service.extract_graph_from_chunks(chunks, completed_batches=cached))
        self.service.extract_graph_from_text.assert_not_awaited()
        self.assertTrue(graph.global_linking_processed)
        self.assertFalse(graph.global_linking_succeeded)
        self.assertTrue(resumed.global_linking_processed)
        self.assertEqual(checkpoint.await_count, 3)

    def test_global_failure_diagnostics_do_not_discard_the_section_graph(self):
        self.service.extract_graph_from_text = AsyncMock(side_effect=[self.graph(0), self.graph(1),
            GraphStructureError("private error", reason=GraphFailureReason.INVALID_ENDPOINTS)])
        with patch("app.core.config.settings.graph_global_linking_enabled", True):
            graph = asyncio.run(self.service.extract_graph_from_chunks([self.chunk(0), self.chunk(1)]))
        self.assertEqual(len(graph.nodes), 2)
        self.assertEqual(graph.batches_succeeded, 2)
        self.assertFalse(graph.global_linking_processed)
        self.assertEqual(graph.batch_diagnostics[0].kind, "global")
        self.assertEqual(graph.batch_diagnostics[0].category, "invalid_endpoints")
        self.assertNotIn("private error", graph.model_dump_json())

    def test_repair_can_be_disabled_without_a_hidden_extra_request(self):
        with patch("app.core.config.settings.graph_json_repair_enabled", False), \
             patch.object(self.service, "_extract_with_groq", side_effect=GraphStructureError("invalid")) as groq:
            with self.assertRaises(GraphStructureError):
                asyncio.run(self.service.extract_graph_from_text("evidence"))
        groq.assert_called_once()

    def test_version_four_completed_linking_disables_noop_retry(self):
        upload = SimpleNamespace(stage="READY", graph_status="GRAPH_PARTIAL", graph_node_count=2,
            attempt_count=2, result_json={"graph_batches_failed": 0, "graph_batches_skipped": 0,
            "graph_relationship_pass_version": 4, "graph_global_linking_processed": True})
        self.assertFalse(UploadService.can_reprocess(upload))
        upload.result_json["graph_batches_failed"] = 1
        self.assertTrue(UploadService.can_reprocess(upload))

    def test_repair_metrics_count_each_call_without_provider_text(self):
        metrics = ProviderMetrics()
        with self.assertRaises(GraphStructureError):
            metrics.observe("groq", "graph_extraction",
                lambda: (_ for _ in ()).throw(GraphStructureError("secret text", reason=GraphFailureReason.MALFORMED_JSON)),
                input_characters=10)
        metrics.observe("groq", "graph_extraction", lambda: self.graph(), input_characters=10, repair=True)
        snapshot = metrics.snapshot()
        self.assertEqual(snapshot["totals"]["provider_attempts"], 2)
        self.assertEqual(snapshot["totals"]["repair_attempts"], 1)
        self.assertEqual(snapshot["recent_events"][0]["failure_reason"], "malformed_json")
        self.assertNotIn("secret text", str(snapshot))

    def test_gemini_ignores_thought_parts_before_graph_parsing(self):
        with httpx.Client(base_url=GeminiService.BASE_URL, transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"candidates": [{"content": {"parts": [
                {"text": "private reasoning", "thought": True},
                {"text": '{"nodes": [], "relationships": []}'},
            ]}, "finishReason": "STOP"}]}))) as client, \
             patch("app.core.config.settings.gemini_api_key", "test"), \
             patch("app.services.ingestion_service.gemini_service", GeminiService(client)):
            result = self.service._extract_with_gemini("evidence")
        self.assertTrue(result.empty_extraction)

    def test_gemini_truncated_json_is_not_saved_as_an_empty_graph(self):
        with httpx.Client(base_url=GeminiService.BASE_URL, transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"candidates": [{"content": {"parts": [
                {"text": '{"nodes": []'},
            ]}, "finishReason": "MAX_TOKENS"}]}))) as client, \
             patch("app.core.config.settings.gemini_api_key", "test"), \
             patch("app.services.ingestion_service.gemini_service", GeminiService(client)):
            with self.assertRaises(GraphStructureError) as caught:
                self.service._extract_with_gemini("evidence")
        self.assertEqual(caught.exception.reason, "truncated_response")

    def test_groq_truncation_is_diagnosed_and_client_is_closed(self):
        client = Mock()
        client.chat.completions.create.return_value = SimpleNamespace(choices=[SimpleNamespace(
            finish_reason="length", message=SimpleNamespace(content='{"nodes": []'))])
        with patch("app.services.ingestion_service.Groq", return_value=client):
            with self.assertRaises(GraphStructureError) as caught:
                self.service._extract_with_groq("evidence", allow_repair=False)
        self.assertEqual(caught.exception.reason, "truncated_response")
        client.close.assert_called_once()

    def test_repair_context_budget_counts_sources_instead_of_paragraphs(self):
        excerpts = [f"[Source chunk ID: chunk-{i} | Page: {i + 1}]\n"
                    + (f"Evidence for source {i}. " * 20 + "\n\n") * 30
                    for i in range(8)]
        contexts = self.service._graph_extraction_contexts("\n\n".join(excerpts))
        for limit, context in zip((5000, 3000), contexts, strict=True):
            self.assertLessEqual(len(context), limit)
            for i in range(8):
                self.assertIn(f"Source chunk ID: chunk-{i}", context)
                self.assertIn(f"Evidence for source {i}", context)

    def test_unstructured_single_line_context_is_still_bounded(self):
        contexts = self.service._graph_extraction_contexts("word " * 6000)
        self.assertLessEqual(len(contexts[0]), 5000)
        self.assertLessEqual(len(contexts[-1]), 3000)


if __name__ == "__main__":
    unittest.main()
