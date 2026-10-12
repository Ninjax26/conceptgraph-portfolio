import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch, AsyncMock

import httpx
from pydantic import SecretStr

from app.services.evidence_facets import comparison_facets
from app.services.rerank_service import RerankService
from app.services.citation_service import prepare_evidence


class EvidenceFacetTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch("app.core.config.settings.evidence_min_score", .35))
        self.enterContext(patch("app.core.config.settings.retrieval_verify_identifiers", True))

    @staticmethod
    def chunk(text, upload="a", page=1):
        return {"id": f"{upload}-{page}", "text": text, "score": .6,
                "metadata": {"upload_id": upload, "page_number": page}}

    def service(self):
        service = RerankService()
        service._model = SimpleNamespace(predict=Mock(side_effect=lambda pairs: [
            4. if query.casefold().split()[0] in passage.casefold() else -10.
            for query, passage in pairs]))
        return service

    def test_ambiguous_qualified_and_nested_comparisons_keep_original_query(self):
        for query in ("Compare A with B: explain why", "Compare A with B with C",
                      "Compare A and B", "Explain A with examples"):
            self.assertEqual(comparison_facets(query), [])

    def test_local_facets_are_one_batch_and_cover_both_documents(self):
        service = self.service()
        chunks = [self.chunk("TLS checks certificate identity."),
                  self.chunk("Authorization checks document permissions.", "b")]
        ranked = service._rerank_locally("Compare TLS with authorization.", chunks)
        self.assertEqual(service._model.predict.call_count, 1)
        self.assertEqual(len(ranked), 4)
        sources, confidence = prepare_evidence("Compare TLS with authorization.", ranked)
        self.assertEqual({source["document_id"] for source in sources}, {"a", "b"})
        self.assertEqual(confidence["evidence_count"], 2)
        self.assertTrue(all("_evidence_facets" not in source for source in sources))
        self.assertNotIn("evidence_facet", chunks[0])

    def test_one_supported_side_cannot_unlock_generation_evidence(self):
        service = self.service()
        ranked = service._rerank_locally("Compare TLS with baking.", [self.chunk("TLS checks identity.")])
        sources, confidence = prepare_evidence("Compare TLS with baking.", ranked)
        self.assertEqual(sources, [])
        self.assertEqual(confidence["level"], "insufficient")

    def test_identifiers_are_checked_for_each_facet_not_just_globally(self):
        ranked = [dict(self.chunk("PMP-104 pressure is 11 bar."), rerank_score=4.,
                       evidence_facet=index, evidence_facet_count=2) for index in (0, 1)]
        sources, _ = prepare_evidence("Compare PMP-104 pressure with PMP-105 pressure.", ranked)
        self.assertEqual(sources, [])

    def test_facet_reservation_survives_five_higher_scoring_citations(self):
        ranked = [dict(self.chunk(f"TLS certificate note {index}.", page=index), rerank_score=6.,
                       evidence_facet=0, evidence_facet_count=2) for index in range(1, 7)]
        ranked.append(dict(self.chunk("Authorization protects documents.", "b"), rerank_score=3.,
                           evidence_facet=1, evidence_facet_count=2))
        sources, _ = prepare_evidence("Compare TLS with authorization.", ranked)
        self.assertEqual(len(sources), 5)
        self.assertIn("b", {source["document_id"] for source in sources})
        self.assertEqual([source["source_id"] for source in sources], [f"source-{i}" for i in range(1, 6)])

    def test_same_literal_source_covering_both_facets_is_not_duplicated(self):
        ranked = [dict(self.chunk("TLS and authorization protect different things."), rerank_score=4.,
                       evidence_facet=index, evidence_facet_count=2) for index in (0, 1)]
        sources, confidence = prepare_evidence("Compare TLS with authorization.", ranked)
        self.assertEqual(len(sources), 1)
        self.assertEqual(confidence["evidence_count"], 1)

    def test_noncomparison_hosted_call_count_remains_one_and_comparison_is_two(self):
        async def check():
            calls = []
            def response(request):
                body = json.loads(request.content)
                calls.append(body["query"])
                return httpx.Response(200, json={"results": [
                    {"index": index, "relevance_score": .9}
                    for index in range(len(body["documents"]))]})
            async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
                with patch("app.core.config.settings.cohere_api_key", SecretStr("test")):
                    service = RerankService(client)
                    await service._rerank_with_cohere("Explain TLS", [self.chunk("TLS context.")])
                    self.assertEqual(len(calls), 1)
                    await service._rerank_with_cohere("Compare TLS with authorization.", [self.chunk("TLS and authorization context.")])
                    self.assertEqual(calls, ["Explain TLS", "TLS", "authorization"])
                    self.assertFalse(client.is_closed)
        asyncio.run(check())

    def test_second_hosted_facet_error_does_not_return_partial_results(self):
        async def check():
            calls = []
            def response(request):
                calls.append(request)
                return (httpx.Response(200, json={"results": [{"index": 0, "relevance_score": .9}]})
                        if len(calls) == 1 else httpx.Response(429))
            async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
                with patch("app.core.config.settings.cohere_api_key", SecretStr("test")):
                    with self.assertRaises(httpx.HTTPStatusError):
                        await RerankService(client)._rerank_with_cohere("Compare TLS with authorization.", [self.chunk("Context.")])
        asyncio.run(check())

    def test_owned_hosted_clients_close_even_when_second_facet_fails(self):
        async def check():
            first = SimpleNamespace(post=AsyncMock(return_value=httpx.Response(200, request=httpx.Request("POST", "https://example.test"),
                json={"results": [{"index": 0, "relevance_score": .9}]})), aclose=AsyncMock())
            second = SimpleNamespace(post=AsyncMock(side_effect=TimeoutError("timeout")), aclose=AsyncMock())
            with patch("app.core.config.settings.cohere_api_key", SecretStr("test")), \
                 patch("app.services.rerank_service.httpx.AsyncClient", side_effect=[first, second]):
                with self.assertRaises(TimeoutError):
                    await RerankService()._rerank_with_cohere("Compare TLS with authorization.", [self.chunk("Context.")])
            first.aclose.assert_awaited_once()
            second.aclose.assert_awaited_once()
        asyncio.run(check())

    def test_missing_comparison_side_does_not_call_synthesis_at_api_boundary(self):
        async def check():
            from app.api.endpoints import query
            ranked = self.service()._rerank_locally("Compare TLS with baking.", [self.chunk("TLS checks identity.")])
            retrieval = SimpleNamespace(retrieve=AsyncMock(return_value={"chunks": ranked,
                                        "graph_context": [], "graph_metadata": {}}))
            with patch.object(query.CourseService, "get_ready_context", AsyncMock(return_value=SimpleNamespace())), \
                 patch.object(query, "get_rerank_service", return_value=SimpleNamespace(rerank=AsyncMock(return_value=ranked))), \
                 patch.object(query, "get_synthesis_service") as generation:
                result = await query.query_conceptgraph(query.QueryRequest(question="Compare TLS with baking.", course_id="course"), retrieval, None)
            generation.assert_not_called()
            self.assertEqual(result.generation_metadata.provider_used, "not_attempted")
        asyncio.run(check())
