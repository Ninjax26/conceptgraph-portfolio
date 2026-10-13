"""Offline contracts for compatible, source-bound graph checkpoint reuse."""

import asyncio
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from pydantic import SecretStr

from app.core.exceptions import GraphStructureError, LLMProviderRateLimitError
from app.schemas.extraction import ConceptNode, ConceptRelationship, GraphExtractionResponse
from app.services.ingestion_service import IngestionService
from app.services.parser_service import DocumentChunk


class GraphCheckpointCompatibilityTests(unittest.TestCase):
    def setUp(self):
        for key, value in {
            "llm_provider": "groq", "groq_api_key": "test-key-a",
            "groq_model": "test-model-a", "gemini_api_key": "test-backup-key",
            "gemini_model": "test-model-b", "cerebras_api_key": None,
            "graph_global_linking_enabled": False, "graph_json_repair_enabled": True,
        }.items():
            self.enterContext(patch(f"app.core.config.settings.{key}", value))
        self.service = IngestionService(SimpleNamespace(), SimpleNamespace())
        self.chunk = DocumentChunk(id="chunk-a", text="DNS maps a domain name to an IP address.",
            metadata={"document_name": "network.pdf", "upload_id": "upload-a",
                      "page_number": 2, "section_heading": "DNS", "execution_token": "task-a"})

    def batch(self, chunk=None):
        return self.service._graph_section_batches([chunk or self.chunk])[0]

    def graph(self):
        return GraphExtractionResponse(nodes=[ConceptNode(
            id="dns", name="DNS", type="concept", source_chunk_id=self.chunk.id)])

    def run_graph(self, checkpoints):
        return asyncio.run(self.service.extract_graph_from_chunks(
            [self.chunk], completed_batches=checkpoints))

    def test_keys_are_stable_and_fit_existing_database_column(self):
        key = self.batch().batch_key
        self.assertEqual(key, self.batch().batch_key)
        self.assertTrue(key.startswith("section:v2:"))
        self.assertLessEqual(len(key), 80)

    def test_configured_model_changes_invalidate(self):
        initial = self.batch().batch_key
        for model in ("groq_model", "gemini_model"):
            with patch(f"app.core.config.settings.{model}", "different-model"):
                self.assertNotEqual(initial, self.batch().batch_key)

    def test_prompt_schema_and_validator_version_changes_invalidate(self):
        initial = self.batch().batch_key
        for name, value in (("_extraction_system_prompt", "changed prompt"),
                            ("_graph_extraction_schema", {"type": "object"})):
            with patch.object(IngestionService, name, return_value=value):
                self.assertNotEqual(initial, self.batch().batch_key)
        with patch.object(IngestionService, "GRAPH_CHECKPOINT_CONTRACT_VERSION", 2):
            self.assertNotEqual(initial, self.batch().batch_key)

    def test_context_output_and_repair_changes_invalidate(self):
        initial = self.batch().batch_key
        for name, value in (("GRAPH_CONTEXT_LIMITS", (4000, 2000)), ("MAX_GRAPH_OUTPUT_TOKENS", 1000)):
            with patch.object(IngestionService, name, value):
                self.assertNotEqual(initial, self.batch().batch_key)
        with patch("app.core.config.settings.graph_json_repair_enabled", False):
            self.assertNotEqual(initial, self.batch().batch_key)

    def test_route_uses_same_primary_and_backup_precedence_as_generation(self):
        self.assertEqual(self.service._graph_provider_route(), ["groq", "gemini"])
        initial = self.batch().batch_key
        with patch("app.core.config.settings.cerebras_api_key", SecretStr("test-third-key")):
            self.assertEqual(initial, self.batch().batch_key)  # unused third provider
            with patch("app.core.config.settings.gemini_api_key", None):
                self.assertEqual(self.service._graph_provider_route(), ["groq", "cerebras"])
                self.assertNotEqual(initial, self.batch().batch_key)
        with patch("app.core.config.settings.llm_provider", "gemini"):
            self.assertEqual(self.service._graph_provider_route(), ["gemini"])
            self.assertNotEqual(initial, self.batch().batch_key)

    def test_secret_rotation_quota_and_task_changes_do_not_invalidate(self):
        initial = self.batch().batch_key
        with patch("app.core.config.settings.groq_api_key", "test-key-rotated"), \
             patch("app.core.config.settings.gemini_api_key", "test-backup-rotated"), \
             patch("app.core.config.settings.graph_max_batches", 1), \
             patch("app.core.config.settings.provider_requests_per_minute", 1):
            self.assertEqual(initial, self.batch().batch_key)
        changed = DocumentChunk(id=self.chunk.id, text=self.chunk.text,
                                metadata={**self.chunk.metadata, "execution_token": "task-b"})
        self.assertEqual(initial, self.batch(changed).batch_key)

    def test_source_text_identity_and_provenance_changes_invalidate(self):
        initial = self.batch().batch_key
        changes = [DocumentChunk(id="chunk-b", text=self.chunk.text, metadata=self.chunk.metadata),
                   DocumentChunk(id=self.chunk.id, text="Changed lesson.", metadata=self.chunk.metadata)]
        for field, value in (("page_number", 3), ("section_heading", "Routing"),
                             ("document_name", "other.pdf"), ("upload_id", "upload-b")):
            changes.append(DocumentChunk(id=self.chunk.id, text=self.chunk.text,
                                         metadata={**self.chunk.metadata, field: value}))
        for changed in changes:
            self.assertNotEqual(initial, self.batch(changed).batch_key)

    def test_compatible_serialized_checkpoint_avoids_model_call(self):
        checkpoint = self.service.decode_graph_checkpoint(json.loads(self.graph().model_dump_json()))
        self.service.extract_graph_from_text = AsyncMock(side_effect=AssertionError("Unexpected model call"))
        result = self.run_graph({self.batch().batch_key: checkpoint})
        self.service.extract_graph_from_text.assert_not_awaited()
        self.assertEqual(result.batches_succeeded, 1)
        self.assertEqual(result.nodes[0].page_number, 2)

    def test_model_change_regenerates_instead_of_reusing_stale_checkpoint(self):
        key = self.batch().batch_key
        self.service.extract_graph_from_text = AsyncMock(return_value=self.graph())
        with patch("app.core.config.settings.groq_model", "changed-model"):
            self.run_graph({key: self.graph()})
        self.service.extract_graph_from_text.assert_awaited_once()

    def test_unversioned_checkpoint_is_retained_but_not_trusted(self):
        batch = self.batch()
        digest = hashlib.sha256(batch.section_key.encode())
        digest.update(b"\0" + self.chunk.id.encode() + b"\0" + self.chunk.text.encode())
        checkpoints = {f"section:{digest.hexdigest()}": self.graph()}
        before = dict(checkpoints)
        self.service.extract_graph_from_text = AsyncMock(return_value=self.graph())
        self.run_graph(checkpoints)
        self.service.extract_graph_from_text.assert_awaited_once()
        self.assertEqual(checkpoints, before)

    def test_cached_provenance_is_rebound_not_used_as_authority(self):
        poisoned = self.graph().model_copy(update={"nodes": [self.graph().nodes[0].model_copy(update={
            "upload_id": "other-upload", "document_name": "private.pdf", "page_number": 99,
            "source_chunk_ids": ["other-chunk"], "page_numbers": [99],
        })]})
        self.service.extract_graph_from_text = AsyncMock(side_effect=AssertionError("Unexpected model call"))
        result = self.run_graph({self.batch().batch_key: poisoned})
        node = result.nodes[0]
        self.assertEqual((node.upload_id, node.document_name, node.page_number), ("upload-a", "network.pdf", 2))
        self.assertEqual(node.source_chunk_ids, ["chunk-a"])
        self.assertEqual(node.page_numbers, [2])
        self.assertEqual(poisoned.nodes[0].upload_id, "other-upload")  # no mutation

    def test_unknown_cached_sources_are_regenerated(self):
        bad = self.graph().model_copy(update={"nodes": [self.graph().nodes[0].model_copy(
            update={"source_chunk_id": "invented-source"})]})
        self.service.extract_graph_from_text = AsyncMock(return_value=self.graph())
        self.run_graph({self.batch().batch_key: bad})
        self.service.extract_graph_from_text.assert_awaited_once()

    def test_raw_unsupported_edges_and_missing_endpoints_are_rejected(self):
        payload = self.graph().model_dump(mode="json")
        for edge in (
            {"source_node_id": "dns", "target_node_id": "dns", "relation_type": "MADE_UP"},
            {"source_node_id": "dns", "target_node_id": "missing", "relation_type": "PART_OF"},
        ):
            with self.assertRaises(GraphStructureError):
                self.service.decode_graph_checkpoint({**payload, "relationships": [edge]})

    def test_corrupted_cached_graph_is_regenerated(self):
        edge = ConceptRelationship(source_node_id="dns", target_node_id="missing", relation_type="PART_OF")
        bad = self.graph().model_copy(update={"relationships": [edge]})
        self.service.extract_graph_from_text = AsyncMock(return_value=self.graph())
        self.run_graph({self.batch().batch_key: bad})
        self.service.extract_graph_from_text.assert_awaited_once()

    def test_quota_limited_output_is_not_a_completed_batch(self):
        bad = self.graph().model_copy(update={"provider_limited": True, "provider_retry_after_seconds": 120})
        self.service.extract_graph_from_text = AsyncMock(return_value=self.graph())
        self.run_graph({self.batch().batch_key: bad})
        self.service.extract_graph_from_text.assert_awaited_once()

    def test_explicit_empty_success_is_reused_but_implicit_empty_is_not(self):
        self.service.extract_graph_from_text = AsyncMock(return_value=GraphExtractionResponse())
        explicit = GraphExtractionResponse(empty_extraction=True)
        result = self.run_graph({self.batch().batch_key: explicit})
        self.service.extract_graph_from_text.assert_not_awaited()
        self.assertEqual(result.sections_empty, 1)
        self.run_graph({self.batch().batch_key: GraphExtractionResponse()})
        self.service.extract_graph_from_text.assert_awaited_once()

    def test_recovery_key_includes_preceding_concept_names(self):
        chunks = [self.chunk]
        first = self.service._relationship_recovery_context(chunks, self.graph().nodes)
        changed = self.service._relationship_recovery_context(chunks, [self.graph().nodes[0].model_copy(
            update={"name": "Domain Name System"})])
        first_key = self.service._checkpoint_key("global:v5", "__global__", chunks, recovery_context=first)
        changed_key = self.service._checkpoint_key("global:v5", "__global__", chunks, recovery_context=changed)
        self.assertNotEqual(first_key, changed_key)
        self.assertLessEqual(len(first_key), 80)

    def test_quota_resume_calls_only_unfinished_compatible_batch(self):
        second = DocumentChunk(id="chunk-b", text="TCP carries application data.",
            metadata={**self.chunk.metadata, "section_heading": "TCP", "page_number": 3})
        saved = {}
        async def save(batch, extraction):
            saved[batch.batch_key] = self.service.decode_graph_checkpoint(json.loads(extraction.model_dump_json()))
        self.service.extract_graph_from_text = AsyncMock(side_effect=[self.graph(), LLMProviderRateLimitError("quota")])
        asyncio.run(self.service.extract_graph_from_chunks([self.chunk, second], on_batch_completed=save))
        self.service.extract_graph_from_text = AsyncMock(return_value=GraphExtractionResponse(nodes=[
            ConceptNode(id="tcp", name="TCP", type="concept", source_chunk_id="chunk-b")]))
        result = asyncio.run(self.service.extract_graph_from_chunks([self.chunk, second], completed_batches=saved))
        self.service.extract_graph_from_text.assert_awaited_once()
        self.assertEqual(result.batches_succeeded, 2)
        self.assertFalse(result.provider_limited)

    def test_serialized_checkpoint_reuses_in_a_fresh_process(self):
        payload = json.dumps({"key": self.batch().batch_key, "graph": self.graph().model_dump(mode="json")})
        code = """
import json, sys
from unittest.mock import AsyncMock
from tests.test_graph_checkpoint_compatibility import GraphCheckpointCompatibilityTests
test = GraphCheckpointCompatibilityTests()
test.setUp()
try:
    saved = json.loads(sys.argv[1])
    test.service.extract_graph_from_text = AsyncMock(side_effect=AssertionError('Unexpected provider call'))
    result = test.run_graph({saved['key']: test.service.decode_graph_checkpoint(saved['graph'])})
    print(json.dumps({'calls': test.service.extract_graph_from_text.await_count,
                      'batches': result.batches_succeeded, 'page': result.nodes[0].page_number}))
finally:
    test.doCleanups()
"""
        child = subprocess.run([sys.executable, "-c", code, payload],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, check=True, timeout=30)
        self.assertEqual(json.loads(child.stdout), {"calls": 0, "batches": 1, "page": 2})

    def test_recovery_recomputes_when_section_concepts_change(self):
        second = DocumentChunk(id="chunk-b", text="TCP carries application data.",
            metadata={**self.chunk.metadata, "section_heading": "TCP", "page_number": 3})
        tcp = GraphExtractionResponse(nodes=[ConceptNode(id="tcp", name="TCP", type="concept", source_chunk_id="chunk-b")])
        saved = {}
        async def save(batch, extraction):
            saved[batch.batch_key] = extraction
        with patch("app.core.config.settings.graph_global_linking_enabled", True):
            self.service.extract_graph_from_text = AsyncMock(side_effect=[self.graph(), tcp, GraphExtractionResponse()])
            asyncio.run(self.service.extract_graph_from_chunks([self.chunk, second], on_batch_completed=save))
            section_key = self.batch().batch_key
            saved[section_key] = saved[section_key].model_copy(update={"nodes": [
                saved[section_key].nodes[0].model_copy(update={"name": "Domain Name System"})]})
            self.service.extract_graph_from_text = AsyncMock(return_value=GraphExtractionResponse())
            result = asyncio.run(self.service.extract_graph_from_chunks([self.chunk, second], completed_batches=saved))
        self.service.extract_graph_from_text.assert_awaited_once()
        self.assertTrue(result.global_linking_processed)
        self.assertIn("Domain Name System", self.service.extract_graph_from_text.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
