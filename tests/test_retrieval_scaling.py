"""Retrieval budgets, identifier scope, metric correctness, and invalid scores."""
import asyncio
import json
from types import SimpleNamespace
import unittest
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import httpx
from pydantic import SecretStr
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from app.services.citation_service import assess_evidence, build_sources, prepare_evidence
from app.services.rag_service import RetrievalService
from app.services.rerank_service import RerankService
from app.services.retrieval_candidates import fuse_candidates, identifier_supported, identifier_terms
from scripts.run_evaluation import load_dataset, score_retrieval_result, summarize_retrieval


class RetrievalScalingTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch("app.core.config.settings.embedding_provider", "local"))
        self.enterContext(patch("app.core.config.settings.retrieval_identifier_lookup", True))
        self.client = QdrantClient(":memory:")
        self.addCleanup(self.client.close)
        self.service = RetrievalService(SimpleNamespace(), self.client)
        self.client.create_collection(self.service.collection_name, vectors_config=VectorParams(size=2, distance=Distance.COSINE))
        self.service._embedding_model = SimpleNamespace(encode=Mock(return_value=SimpleNamespace(tolist=lambda: [1., 0.])))

    def points(self):
        self.client.upsert(self.service.collection_name, points=[
            PointStruct(id=1, vector=[1., 0.], payload={"upload_id": "allowed", "text": "General pump description.", "identifier_terms": []}),
            PointStruct(id=2, vector=[.7, .7], payload={"upload_id": "allowed", "text": "PMP-104 inspection every 30 days.", "identifier_terms": ["pmp104"]}),
            PointStruct(id=3, vector=[1., 0.], payload={"upload_id": "private", "text": "Private PMP-104 information.", "identifier_terms": ["pmp104"]}),
        ])

    def test_identifier_extraction_is_bounded_and_ignores_plain_words(self):
        self.assertEqual(identifier_terms("PMP-104, PMP-104, TLS1.3 and HTTP 401", 3), ["pmp104", "tls1.3", "http401"])
        self.assertEqual(identifier_terms("Explain two-hop retrieval"), [])
        self.assertEqual(len(identifier_terms(" ".join(f"EQ-{i}" for i in range(1000)))), 64)

    def test_separator_variants_work_through_lookup_and_evidence_selection(self):
        self.points()
        self.assertEqual(identifier_terms("PMP 104"), identifier_terms("PMP-104"))
        self.assertEqual(identifier_terms("PMP_104"), identifier_terms("PMP104"))
        self.assertEqual(identifier_terms("PMP\u2011104"), identifier_terms("PMP104"))
        chunks = self.service.search_qdrant("Inspect PMP 104", [], [], ["allowed"], 2)
        chunks = [dict(chunk, rerank_score=8.) for chunk in chunks]
        with patch("app.core.config.settings.retrieval_verify_identifiers", True):
            sources, _ = prepare_evidence("Inspect PMP 104", chunks)
        self.assertTrue(any("PMP-104" in source["supporting_passage"] for source in sources))

    def test_question_scenario_quantities_do_not_become_required_equipment_ids(self):
        chunk = {"text": "PMP-104 has a pressure limit of 11.0 bar. Stop above that limit.",
                 "score": .9, "rerank_score": 8., "metadata": {}}
        with patch("app.core.config.settings.retrieval_verify_identifiers", True):
            sources, _ = prepare_evidence("For PMP-104, what if pressure reaches 15.0 bar?", [chunk])
        self.assertEqual(len(sources), 1)
        self.assertEqual(identifier_terms("15.0 bar", named_only=True), [])

    def test_cohere_rejects_indices_that_could_map_to_the_wrong_chunk(self):
        async def check(index):
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(
                200, json={"results": [{"index": index, "relevance_score": .9}]}))) as client:
                with patch("app.core.config.settings.cohere_api_key", SecretStr("test")):
                    with self.assertRaises(RuntimeError):
                        await RerankService(client)._rerank_with_cohere("Question", [{"text": "Evidence"}])
        for index in (False, .5, "0", -1, 2):
            asyncio.run(check(index))

    def test_local_reranker_sees_a_bounded_relevant_tail_without_mutating_chunks(self):
        text = "Background material. " * 200 + "PMP-104 has an approved pressure limit of 11.0 bar."
        service = RerankService()
        predict = Mock(return_value=[5.])
        service._model = SimpleNamespace(predict=predict)
        chunks = [{"text": text, "score": .7}]
        result = service._rerank_locally("PMP104 pressure?", chunks)
        pair = predict.call_args.args[0][0]
        self.assertIn("11.0 bar", pair[1])
        self.assertLessEqual(len(pair[1]), 1600)
        self.assertEqual(result[0]["text"], text)
        self.assertNotIn("rerank_score", chunks[0])

    def test_identifier_lookup_obeys_the_same_upload_scope_as_vectors(self):
        self.points()
        metadata = {}
        chunks = self.service.search_qdrant("Inspect PMP-104", [], [], ["allowed"], 2, metadata=metadata)
        self.assertEqual({chunk["metadata"]["upload_id"] for chunk in chunks}, {"allowed"})
        self.assertTrue(metadata["identifier_lookup_used"])
        self.assertEqual(metadata["identifier_count"], 1)

    def test_queries_without_identifiers_make_no_scroll_request(self):
        self.points()
        with patch.object(self.client, "scroll") as scroll:
            self.service.search_qdrant("Describe the pump", [], [], ["allowed"], 2)
        scroll.assert_not_called()

    def test_empty_scope_does_not_embed_or_query(self):
        self.assertEqual(self.service.search_qdrant("PMP-104", [], [], [], 2), [])
        self.service._embedding_model.encode.assert_not_called()

    def test_vector_timings_separate_local_embedding_and_store_calls(self):
        self.points()
        metadata = {}
        chunks = self.service.search_qdrant("PMP-104", [], [], ["allowed"], 2, metadata=metadata)
        self.assertTrue(chunks)
        self.assertEqual(metadata["embedding_execution"], "local")
        self.assertEqual(set(metadata["timings"]), {"collection_check_ms", "query_embedding_ms", "dense_search_ms", "identifier_lookup_ms"})
        self.assertTrue(all(value >= 0 for value in metadata["timings"].values()))

    def test_hosted_embedding_is_labelled_as_part_of_the_dense_request(self):
        metadata = {}
        with patch("app.core.config.settings.embedding_provider", "qdrant_cloud"), \
             patch.object(self.client, "query_points", return_value=SimpleNamespace(points=[])):
            self.service.search_qdrant("Describe the pump", [], [], ["allowed"], 2, metadata=metadata)
        self.assertEqual(metadata["embedding_execution"], "hosted_in_dense_search")
        self.service._embedding_model.encode.assert_not_called()

    def test_optional_identifier_failure_preserves_dense_candidates(self):
        self.points()
        metadata = {}
        with patch.object(self.client, "scroll", side_effect=RuntimeError("private detail")):
            chunks = self.service.search_qdrant("PMP-104", [], [], ["allowed"], 2, metadata=metadata)
        self.assertEqual(len(chunks), 2)
        self.assertEqual(metadata["fallback_reason"], "identifier_lookup_unavailable")
        self.assertNotIn("private detail", str(metadata))

    def test_legacy_vectors_without_identifier_metadata_remain_searchable(self):
        self.client.upsert(self.service.collection_name, points=[PointStruct(id=1, vector=[1., 0.], payload={"upload_id": "allowed", "text": "PMP-104"})])
        chunks = self.service.search_qdrant("PMP-104", [], [], ["allowed"], 2)
        self.assertEqual(len(chunks), 1)

    def test_rank_fusion_preserves_cosine_score_and_deduplicates_points(self):
        dense = [{"id": "a", "score": .8}, {"id": "b", "score": .6}]
        lexical = [{"id": "b", "score": None}, {"id": "c", "score": None}]
        result = fuse_candidates(dense, lexical, 3)
        self.assertEqual([x["id"] for x in result], ["b", "a", "c"])
        self.assertEqual(result[0]["score"], .6)
        self.assertIsNone(result[-1]["score"])
        self.assertNotIn("fusion_score", dense[0])

    def test_candidate_budget_is_validated(self):
        for limit in (0, 101):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                self.service.search_qdrant("Question", [], [], ["allowed"], limit)

    def test_nonfinite_scores_cannot_create_confident_evidence(self):
        for field in ("score", "rerank_score"):
            for value in (float("nan"), float("inf"), -float("inf")):
                chunk = {"score": .9, "rerank_score": 3., field: value}
                evidence, confidence = assess_evidence([chunk])
                self.assertEqual(evidence, [])
                self.assertEqual(confidence["level"], "insufficient")

    def test_cohere_rejects_invalid_relevance_scores(self):
        async def check(score):
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(
                200, content=json.dumps({"results": [{"index": 0, "relevance_score": score}]}).encode()))) as client:
                with patch("app.core.config.settings.cohere_api_key", SecretStr("test")):
                    with self.assertRaises(RuntimeError):
                        await RerankService(client)._rerank_with_cohere("Question", [{"text": "Evidence"}])
        for score in (float("nan"), float("inf"), -.1, 1.1):
            asyncio.run(check(score))

    def test_local_reranker_rejects_nonfinite_output(self):
        service = RerankService()
        service._model = SimpleNamespace(predict=lambda pairs: [float("nan")])
        with self.assertRaises(RuntimeError):
            service._rerank_locally("Question", [{"text": "Evidence"}])

    def test_identifier_matching_does_not_accept_another_equipment_prefix(self):
        for text in ("PMP-104", "pmp_104", "PMP 104", "PMP104"):
            self.assertTrue(identifier_supported("PMP-104", text))
        for text in ("PMP-1049", "PMP-104A", "PMP-105", "AS-PMP-104", "PMP-104-A"):
            self.assertFalse(identifier_supported("PMP-104", text))
        self.assertTrue(identifier_supported("TLS1.3", "Use TLS 1.3"))
        self.assertTrue(identifier_supported("PMP-104", "Equipment PMP-104. Inspect it."))

    def test_missing_identifier_is_refused_despite_high_relevance_scores(self):
        chunks = [{"text": "PMP-104 pressure is 11.0 bar.", "score": .99, "rerank_score": 8.,
                   "metadata": {"upload_id": "allowed", "page_number": 1}}]
        with patch("app.core.config.settings.retrieval_verify_identifiers", True):
            sources, confidence = prepare_evidence("What is the pressure of PMP-99999?", chunks)
        self.assertEqual(sources, [])
        self.assertEqual(confidence["level"], "insufficient")
        self.assertIn("identifiers", confidence["reason"])

    def test_comparison_requires_evidence_for_every_requested_identifier(self):
        chunks = [{"text": f"{identifier} has an inspection interval of {days} days.", "score": .9,
                   "rerank_score": 8., "metadata": {"upload_id": identifier, "page_number": 1}}
                  for identifier, days in (("PMP-104", 60), ("PMP-106", 7))]
        with patch("app.core.config.settings.retrieval_verify_identifiers", True):
            sources, _ = prepare_evidence("Compare PMP-104 and PMP-106", chunks)
            missing, confidence = prepare_evidence("Compare PMP-104 and PMP-106", chunks[:1])
        self.assertEqual(len(sources), 2)
        self.assertEqual(missing, [])
        self.assertEqual(confidence["level"], "insufficient")

    def test_question_focused_excerpt_preserves_relevant_tail_and_page(self):
        text = "Background material. " * 100 + "PMP-104 operating pressure must not exceed 11.0 bar."
        chunk = {"text": text, "metadata": {"upload_id": "allowed", "page_number": 7}}
        sources = build_sources([chunk], question="What is the operating pressure of PMP-104?")
        self.assertIn("11.0 bar", sources[0]["supporting_passage"])
        self.assertLessEqual(len(sources[0]["supporting_passage"]), 900)
        self.assertEqual(sources[0]["page_number"], 7)
        self.assertTrue(sources[0]["preview_url"].endswith("#page=7"))

    def test_different_evidence_with_the_same_prefix_is_not_deduplicated(self):
        prefix = "Shared background. " * 20
        chunks = [{"text": prefix + suffix, "metadata": {"upload_id": "allowed", "page_number": 1}}
                  for suffix in ("Pressure is 11.0 bar.", "Inspection occurs every 60 days.")]
        self.assertEqual(len(build_sources(chunks)), 2)

    def test_identifier_guard_can_be_disabled_for_a_controlled_comparison(self):
        chunk = {"text": "PMP-104 has a pressure limit.", "score": .9, "rerank_score": 8., "metadata": {}}
        with patch("app.core.config.settings.retrieval_verify_identifiers", False):
            sources, _ = prepare_evidence("PMP-99999 pressure?", [chunk])
        self.assertEqual(len(sources), 1)

    def test_more_than_five_sources_cannot_hide_missing_identifier_citations(self):
        chunks = [{"text": f"PMP-{i} pressure is recorded here.", "score": .9, "rerank_score": 8.,
                   "metadata": {"upload_id": str(i), "page_number": 1}} for i in range(101, 107)]
        question = "Compare " + " ".join(f"PMP-{i}" for i in range(101, 107))
        with patch("app.core.config.settings.retrieval_verify_identifiers", True):
            sources, confidence = prepare_evidence(question, chunks)
        self.assertEqual(sources, [])
        self.assertEqual(confidence["level"], "insufficient")

    def test_questions_with_too_many_identifiers_are_not_silently_truncated(self):
        question = "Compare " + " ".join(f"PMP-{i}" for i in range(101, 110))
        chunk = {"text": question, "score": .99, "rerank_score": 8.}
        with patch("app.core.config.settings.retrieval_verify_identifiers", True):
            sources, confidence = prepare_evidence(question, [chunk])
        self.assertEqual(sources, [])
        self.assertIn("smaller set", confidence["reason"])


class QueryEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_identifier_never_calls_answer_generation(self):
        from app.api.endpoints import query
        chunk = {"text": "PMP-104 has a pressure limit of 11.0 bar.", "score": .99,
                 "rerank_score": 8., "metadata": {"upload_id": "allowed", "page_number": 1}}
        service = SimpleNamespace(retrieve=AsyncMock(return_value={"chunks": [chunk], "graph_context": [],
            "graph_metadata": {"retrieval_mode": "vector_only"}}))
        with patch.object(query.CourseService, "get_ready_context", AsyncMock(return_value=SimpleNamespace())), \
             patch.object(query, "get_rerank_service", return_value=SimpleNamespace(rerank=AsyncMock(return_value=[chunk]))), \
             patch.object(query, "get_synthesis_service") as synthesis, \
             patch("app.core.config.settings.retrieval_verify_identifiers", True):
            result = await query.query_conceptgraph(query.QueryRequest(question="PMP-99999 pressure?", course_id="course"), service, None)
        synthesis.assert_not_called()
        self.assertEqual(result.sources, [])
        self.assertEqual(result.generation_metadata.provider_used, "not_attempted")
        self.assertEqual(result.graph_metadata["timings"]["generation_ms"], 0)
        self.assertGreaterEqual(result.graph_metadata["timings"]["query_processing_ms"], 0)


class RetrievalMetricTests(unittest.TestCase):
    def test_bad_labels_are_rejected_before_evaluation(self):
        cases = [
            {"question": "Question", "should_refuse": "false"},
            {"question": "Question", "expected_sources": [{"document_name": "a.pdf", "page_number": True}]},
            {"question": "Question", "expected_sources": [{"document_name": "a.pdf", "page_number": 0}]},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "questions.json"
            for case in cases:
                path.write_text(json.dumps({"questions": [case]}))
                with self.assertRaises(ValueError):
                    load_dataset(path)

    def test_mrr_and_label_precision_use_the_first_relevant_rank(self):
        case = {"question": "Question", "expected_document": "a.pdf", "expected_page": 1}
        wrong = {"metadata": {"document_name": "b.pdf", "page_number": 2}}
        right = {"metadata": {"document_name": "a.pdf", "page_number": 1}}
        row = score_retrieval_result(case, sources=[], confidence={"level": "insufficient"}, graph_metadata={},
            latency_seconds=1, candidates=[wrong, right], ranked_chunks=[right, wrong])
        self.assertEqual(row["raw_retrieval"]["reciprocal_rank_at_5"], .5)
        self.assertEqual(row["reranked_retrieval"]["reciprocal_rank_at_5"], 1.)
        self.assertEqual(row["raw_retrieval"]["label_precision_at_5"], .2)
        self.assertEqual(row["failure_stage"], "evidence_gate")
        self.assertEqual(summarize_retrieval([row])["supported_questions_incorrectly_refused"], 1)

    def test_candidate_pool_miss_is_not_confused_with_a_reranking_miss(self):
        case = {"question": "Question", "expected_document": "a.pdf", "expected_page": 1}
        wrong = {"metadata": {"document_name": "b.pdf", "page_number": 2}}
        right = {"metadata": {"document_name": "a.pdf", "page_number": 1}}
        row = score_retrieval_result(case, sources=[], confidence={}, graph_metadata={},
            latency_seconds=1, candidates=[wrong] * 5 + [right], ranked_chunks=[wrong] * 5 + [right])
        self.assertTrue(row["candidate_pool_all_sources_hit"])
        self.assertEqual(row["failure_stage"], "reranking")

    def test_errors_do_not_inflate_source_recall_or_mrr(self):
        case = {"question": "Question", "expected_document": "a.pdf", "expected_page": 1}
        right = {"metadata": {"document_name": "a.pdf", "page_number": 1}}
        row = score_retrieval_result(case, sources=[], confidence={}, graph_metadata={}, latency_seconds=1,
            candidates=[right], ranked_chunks=[right], error="provider_error")
        metrics = summarize_retrieval([row])["stage_metrics"]
        self.assertEqual(metrics["raw_retrieval"]["mean_source_recall"], 0)
        self.assertEqual(metrics["raw_retrieval"]["mrr_at_5"], 0)

    def test_timing_summary_reports_only_measured_stage_samples(self):
        row = score_retrieval_result({"question": "Unsupported", "should_refuse": True}, sources=[],
            confidence={"level": "insufficient"}, graph_metadata={}, latency_seconds=1,
            stage_timings={"rerank_ms": 20})
        summary = summarize_retrieval([row])
        self.assertEqual(summary["stage_latency_ms"]["rerank_ms"]["mean"], 20)
        self.assertEqual(summary["stage_latency_ms"]["rerank_ms"]["samples"], 1)


if __name__ == "__main__":
    unittest.main()
