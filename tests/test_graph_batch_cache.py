import json
from dataclasses import replace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from app.core.config import settings
from app.core.exceptions import LLMProviderRateLimitError
from app.schemas.extraction import ConceptNode, ConceptRelationship, GraphExtractionResponse
from app.services.graph_batch_cache import CacheEntry, GraphBatchCache, _digest
from app.services.ingestion_service import IngestionService
from app.services.parser_service import DocumentChunk


def chunks(owner):
    return [DocumentChunk(f"{owner}:{number}", f"Literal {heading} information.", {
        "page_number": number, "document_name": f"{owner}.pdf", "upload_id": owner,
        "section_heading": heading,
    }) for number, heading in enumerate(("Ethernet", "DNS"), start=1)]


def authored_graph(request):
    recovery = request.startswith("Relationship recovery")
    names = ["Ethernet", "DNS"] if recovery else ["DNS" if "Literal DNS" in request else "Ethernet"]
    return GraphExtractionResponse(nodes=[ConceptNode(
        id=name, name=name, type="concept", source_chunk_id=f"source-{i}",
    ) for i, name in enumerate(names, start=1)], relationships=[
        ConceptRelationship(source_node_id="Ethernet", target_node_id="DNS", relation_type="RELATED_TO")
    ] if recovery else [])


class GraphBatchCacheTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(patch.object(settings, "graph_batch_cache_enabled", True))
        self.enterContext(patch.object(settings, "graph_global_linking_enabled", True))
        self.cache = GraphBatchCache(None, "fixture")
        self.saved = {}
        self.cache._read = AsyncMock(side_effect=self.saved.get)
        self.service = IngestionService(None, None)
        self.service.extract_graph_from_text = AsyncMock(side_effect=authored_graph)

    async def extract(self, evidence):
        async def save(batch, extraction):
            if batch.cache_entry:
                self.saved[batch.cache_entry.key] = batch.cache_entry
        return await self.service.extract_graph_from_chunks(evidence, batch_cache=self.cache,
                                                           on_batch_completed=save)

    async def test_sections_and_recovery_rebind_after_renamed_duplicate(self):
        await self.extract(chunks("first"))
        self.assertEqual(self.service.extract_graph_from_text.await_count, 3)
        for request in self.service.extract_graph_from_text.await_args_list:
            self.assertNotIn("first", request.args[0])
        for entry in self.saved.values():
            self.assertNotIn("first", json.dumps(entry.payload))
        graph = await self.extract(chunks("second"))
        self.assertEqual(self.cache.hits, 3)
        self.assertEqual(self.service.extract_graph_from_text.await_count, 3)
        self.assertEqual(len(graph.relationships), 1)
        self.assertEqual([(n.upload_id, n.document_name, n.source_chunk_ids, n.page_numbers, n.section_headings)
                          for n in graph.nodes], [
            ("second", "second.pdf", ["second:1"], [1], ["Ethernet"]),
            ("second", "second.pdf", ["second:2"], [2], ["DNS"])])

    async def test_content_metadata_model_and_contract_invalidate(self):
        original = chunks("first")[0]
        await self.extract([original])
        for evidence in (replace(original, text="Different"),
                         replace(original, metadata={**original.metadata, "page_number": 4}),
                         replace(original, metadata={**original.metadata, "section_heading": "Renamed"})):
            await self.extract([evidence])
        with patch.object(IngestionService, "_extraction_system_prompt", return_value="new prompt"):
            await self.extract([original])
        with patch.object(IngestionService, "GRAPH_CHECKPOINT_CONTRACT_VERSION", 55):
            await self.extract([original])
        with patch.object(settings, "llm_provider", "gemini"), patch.object(settings, "gemini_model", "changed-model"):
            await self.extract([original])
        self.assertEqual(self.cache.hits, 0)
        self.assertEqual(self.service.extract_graph_from_text.await_count, 7)

    async def test_source_order_and_recovery_concepts_invalidate(self):
        first, second = chunks("fixture")
        second = replace(second, metadata={**second.metadata, "section_heading": "Ethernet"})
        await self.extract([first, second])
        await self.extract([second, first])
        self.assertEqual(self.cache.hits, 0)
        batch = self.service._graph_section_batches([first, second])[0]
        excerpts = "\n\n".join(self.service._format_graph_excerpt(chunk) for chunk in batch.chunks)
        recovery = replace(batch, kind="global")
        _, saved = await self.cache.extract(self.service, recovery, "Concept A\n\n" + excerpts)
        self.saved[saved.cache_entry.key] = saved.cache_entry
        await self.cache.extract(self.service, recovery, "Concept B\n\n" + excerpts)
        self.assertEqual(self.cache.hits, 0)

    async def test_wrong_sources_extra_fields_broken_digest_and_bad_relationship_regenerate(self):
        evidence = chunks("first")[:1]
        await self.extract(evidence)
        key, valid = next(iter(self.saved.items()))
        for defect in ("unknown", "field", "digest", "relationship"):
            payload = json.loads(json.dumps(valid.payload))
            if defect == "unknown":
                payload["nodes"][0]["source_chunk_id"] = "invalid-id"
            if defect == "field":
                payload["nodes"][0]["upload_id"] = "donor"
            if defect == "relationship":
                payload["relationships"] = [{"source_node_id": "missing", "target_node_id": "DNS",
                                             "relation_type": "RELATED_TO"}]
            digest = "invalid" if defect == "digest" else _digest(key, payload)
            self.saved[key] = CacheEntry(key, payload, digest)
            result = await self.extract(evidence)
            self.assertEqual(result.nodes[0].source_chunk_id, evidence[0].id)
        self.assertEqual(self.cache.hits, 0)
        self.assertEqual(self.service.extract_graph_from_text.await_count, 5)

    async def test_explicit_empty_cache_and_quota_never_saved(self):
        evidence = chunks("first")[:1]
        self.service.extract_graph_from_text.side_effect = LLMProviderRateLimitError("fixture", retry_after_seconds=5)
        self.assertTrue((await self.extract(evidence)).provider_limited)
        self.assertEqual(self.saved, {})
        self.service.extract_graph_from_text.side_effect = None
        self.service.extract_graph_from_text.return_value = GraphExtractionResponse(empty_extraction=True)
        await self.extract(evidence)
        self.assertEqual((await self.extract(evidence)).batches_empty, 1)
        self.assertEqual(self.cache.hits, 1)

    async def test_filtered_and_oversize_batches_keep_fresh_output_only(self):
        evidence = chunks("first")[:1]
        fresh = authored_graph("Ethernet")
        self.service.extract_graph_from_text.side_effect = None
        for extra in [ConceptNode(id="foreign", name="Foreign", type="concept", source_chunk_id="first:1"),
                      ConceptNode(id="large", name="Large", type="concept", source_chunk_id="source-1",
                                  description="0123456789" * 1800)]:
            self.service.extract_graph_from_text.return_value = GraphExtractionResponse(nodes=fresh.nodes + [extra])
            graph = await self.extract(evidence)
            self.assertEqual(len(graph.nodes), 2 if extra.id == "large" else 1)
            self.assertEqual(self.saved, {})
