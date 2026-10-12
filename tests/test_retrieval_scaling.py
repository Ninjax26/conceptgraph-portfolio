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
from app.services.retrieval_candidates import fuse_candidates, identifier_supported, identifier_terms, rerank_passages
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
        predict = Mock(side_effect=lambda pairs: [5.] * len(pairs))
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


class EvidenceSelectionTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch("app.core.config.settings.evidence_min_score", .35))
        self.enterContext(patch("app.core.config.settings.retrieval_verify_identifiers", True))

    @staticmethod
    def chunk(text, upload="allowed", page=1, **scores):
        return {"text": text, "score": .6, "rerank_score": 4.,
                "metadata": {"upload_id": upload, "page_number": page}, **scores}

    def test_identifier_comparison_keeps_complementary_context(self):
        chunks = [self.chunk("HTTP 429 indicates too many requests."),
                  self.chunk("Readiness checks whether dependencies can serve useful traffic.", page=2)]
        sources, confidence = prepare_evidence("How is readiness different from HTTP 429?", chunks)
        self.assertEqual({source["page_number"] for source in sources}, {1, 2})
        self.assertNotEqual(confidence["level"], "insufficient")

    def test_complementary_context_cannot_bypass_missing_identifier(self):
        sources, confidence = prepare_evidence("PMP-99999 pressure?", [
            self.chunk("Pressure limits are recorded in the operating manual.")])
        self.assertEqual(sources, [])
        self.assertEqual(confidence["level"], "insufficient")

    def test_other_equipment_is_not_accepted_as_complementary_context(self):
        sources, _ = prepare_evidence("PMP-104 pressure?", [
            self.chunk("PMP-104 pressure is 11 bar."),
            self.chunk("PMP-105 pressure is 16 bar.", page=2)])
        self.assertEqual(len(sources), 1)
        self.assertNotIn("PMP-105", sources[0]["supporting_passage"])

    def test_complementary_context_still_has_to_pass_threshold(self):
        sources, _ = prepare_evidence("How is readiness different from HTTP 429?", [
            self.chunk("HTTP 429 means slow down."),
            self.chunk("Readiness checks dependencies.", page=2, score=.1, rerank_score=-10.)])
        self.assertEqual(len(sources), 1)

    def test_windows_are_bounded_literal_and_preserve_negation_context(self):
        text = "General introduction. HTTP 429 signals rate limiting. It does not prove readiness. Check dependencies separately."
        windows = rerank_passages(text, "HTTP 429 readiness?")
        self.assertLessEqual(len(windows), 3)
        self.assertTrue(all(window in text and len(window) <= 1600 for window in windows))
        self.assertTrue(any("HTTP 429" in window and "does not" in window for window in windows))

    def test_sentence_windows_do_not_split_decimal_values(self):
        text = "Background information. PMP-104 pressure is 11.0 bar. Do not exceed it. Other background."
        windows = rerank_passages(text, "PMP-104 pressure?")
        self.assertTrue(all("11.0 bar" in window for window in windows))
        self.assertFalse(any(window.startswith("0 bar") for window in windows))

    def test_best_window_becomes_citation_without_mutating_original_chunk(self):
        text = "Unrelated introductory material. Readiness checks dependencies. It does not prove provider quota. Other background."
        chunk = self.chunk(text, page=6)
        service = RerankService()
        def predict(pairs):
            return [4. if passage.startswith("Readiness") else -8. for _, passage in pairs]
        service._model = SimpleNamespace(predict=predict)
        ranked = service._rerank_locally("Readiness provider quota?", [chunk])
        sources, _ = prepare_evidence("Readiness provider quota?", ranked)
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["supporting_passage"], ranked[0]["rerank_passage"])
        self.assertIn("does not prove", sources[0]["supporting_passage"])
        self.assertEqual(sources[0]["page_number"], 6)
        self.assertEqual(ranked[0]["text"], text)
        self.assertNotIn("rerank_passage", chunk)

    def test_citation_identifier_must_exist_in_selected_window(self):
        chunk = self.chunk("PMP-104 pressure is 11 bar. Generic safety guidance.")
        chunk["rerank_passage"] = "Generic safety guidance."
        sources, confidence = prepare_evidence("PMP-104 pressure?", [chunk])
        self.assertEqual(sources, [])
        self.assertEqual(confidence["level"], "insufficient")

    def test_citation_never_uses_a_window_that_is_not_source_text(self):
        chunk = self.chunk("PMP-104 pressure is 11 bar.")
        chunk["rerank_passage"] = "PMP-104 pressure is 99 bar."
        sources, _ = prepare_evidence("PMP-104 pressure?", [chunk])
        self.assertEqual(sources[0]["supporting_passage"], chunk["text"])

    def test_hosted_windows_map_back_to_their_original_documents(self):
        async def check():
            def response(request):
                payload = json.loads(request.content)
                self.assertLessEqual(len(payload["documents"]), 6)
                return httpx.Response(200, json={"results": [
                    {"index": index, "relevance_score": .95 if "TLS protects" in text else .01}
                    for index, text in reversed(list(enumerate(payload["documents"]))) ]})
            async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
                with patch("app.core.config.settings.cohere_api_key", SecretStr("test")):
                    ranked = await RerankService(client)._rerank_with_cohere("TLS protects?", [
                        self.chunk("DNS resolves names.", upload="dns"),
                        self.chunk("Background text. TLS protects data. It checks integrity. More background.", upload="tls")])
            self.assertEqual(len(ranked), 2)
            self.assertEqual(ranked[0]["metadata"]["upload_id"], "tls")
            self.assertIn("TLS protects", ranked[0]["rerank_passage"])
        asyncio.run(check())

    def test_incomplete_hosted_window_response_is_rejected(self):
        async def check():
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(
                    200, json={"results": [{"index": 0, "relevance_score": .9}]}))) as client:
                with patch("app.core.config.settings.cohere_api_key", SecretStr("test")):
                    with self.assertRaisesRegex(RuntimeError, "incomplete"):
                        await RerankService(client)._rerank_with_cohere("TLS integrity?", [self.chunk(
                            "Background text. TLS protects data. It checks integrity. More background.")])
        asyncio.run(check())

    def test_generic_question_does_not_add_unfocused_windows(self):
        self.assertEqual(len(rerank_passages(
            "Background information. More unrelated text. Another sentence.", "What is this?")), 1)

    def test_other_identifier_does_not_spend_extra_window_work(self):
        text = "PMP-105 operating pressure is 16 bar. Record pressure in bar. Stop above this pressure."
        self.assertEqual(rerank_passages(text, "PMP-104 operating pressure?"), [text])

    def test_citation_budget_reserves_evidence_for_each_requested_identifier(self):
        chunks = [self.chunk(f"PMP-104 pressure inspection note {i}.", page=i + 1)
                  for i in range(5)]
        chunks.append(self.chunk("PMP-106 inspection occurs every 7 days.", upload="second", rerank_score=3.))
        sources, confidence = prepare_evidence("Compare PMP-104 and PMP-106 inspection", chunks)
        self.assertEqual(len(sources), 5)
        self.assertEqual({source["document_id"] for source in sources}, {"allowed", "second"})
        self.assertNotEqual(confidence["level"], "insufficient")
        self.assertEqual(sources[0]["page_number"], 1)
        self.assertEqual([source["source_id"] for source in sources],
                         [f"source-{index}" for index in range(1, 6)])
        self.assertNotIn("source_id", chunks[0])

    def test_multi_identifier_coverage_does_not_waste_slots_on_greedy_choice(self):
        required = [f"pmp{i}" for i in range(101, 107)]
        chunks = [self.chunk(text, page=index) for index, text in enumerate([
            "PMP-101 PMP-102 PMP-103 PMP-104 operating instructions.",
            "PMP-101 PMP-102 PMP-105 operating instructions.",
            "PMP-103 PMP-104 PMP-106 operating instructions.",
        ], 1)]
        sources = build_sources(chunks, limit=2, required_identifiers=required)
        self.assertEqual([source["page_number"] for source in sources], [2, 3])
        self.assertTrue(all(any(identifier_supported(term, source["supporting_passage"])
                               for source in sources) for term in required))

    def test_complete_ranked_citations_are_not_replaced_by_lower_ranked_cover(self):
        chunks = [self.chunk(f"PMP-{index} operating instructions.", page=index)
                  for index in range(101, 106)]
        chunks.append(self.chunk("PMP-101 PMP-102 PMP-103 PMP-104 PMP-105 summary.", page=106))
        sources = build_sources(chunks, required_identifiers=[f"pmp{i}" for i in range(101, 106)])
        self.assertEqual([source["page_number"] for source in sources], list(range(101, 106)))

    def test_citation_reservation_never_rescues_below_threshold_evidence(self):
        chunks = [self.chunk(f"PMP-104 inspection note {i}.", page=i + 1) for i in range(5)]
        chunks.append(self.chunk("PMP-106 inspection every 7 days.", upload="second", rerank_score=-10.))
        sources, confidence = prepare_evidence("Compare PMP-104 and PMP-106 inspection", chunks)
        self.assertEqual(sources, [])
        self.assertEqual(confidence["level"], "insufficient")

    def test_non_identifier_questions_keep_ranked_citation_budget(self):
        chunks = [self.chunk(f"Document guidance {i}.", upload=str(i)) for i in range(6)]
        sources, _ = prepare_evidence("Explain the document guidance", chunks)
        self.assertEqual([source["document_id"] for source in sources], [str(i) for i in range(5)])

    def test_selected_excerpt_cannot_hide_conflicting_identifier_in_original_chunk(self):
        conflicting = self.chunk("PMP-104 is listed elsewhere. PMP-105 pressure is 99 bar.", page=2)
        conflicting["rerank_passage"] = "PMP-105 pressure is 99 bar."
        sources, _ = prepare_evidence("PMP-104 pressure?", [
            self.chunk("PMP-104 pressure is 11 bar."), conflicting])
        self.assertEqual(len(sources), 1)
        self.assertNotIn("PMP-105", sources[0]["supporting_passage"])

    def test_missing_identifier_cannot_be_verified_from_unselected_background(self):
        chunk = self.chunk("PMP-104 is listed elsewhere. PMP-105 pressure is 99 bar.")
        chunk["rerank_passage"] = "PMP-105 pressure is 99 bar."
        sources, confidence = prepare_evidence("PMP-104 pressure?", [chunk])
        self.assertEqual(sources, [])
        self.assertEqual(confidence["level"], "insufficient")

    def test_generator_receives_the_exact_passage_scored_by_reranker(self):
        text = "Pressure guidance applies to PMP-104. " + "Background safety discussion. " * 45
        text += "PMP-104 approved pressure is 11.0 bar. Do not exceed this pressure."
        service = RerankService()
        service._model = SimpleNamespace(predict=lambda pairs: [4.] + [-8.] * (len(pairs) - 1))
        ranked = service._rerank_locally("PMP-104 pressure guidance?", [self.chunk(text)])
        sources, _ = prepare_evidence("PMP-104 pressure guidance?", ranked)
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["supporting_passage"], ranked[0]["rerank_passage"])
        self.assertLessEqual(len(ranked[0]["rerank_passage"]), 900)

    def test_final_sentence_window_keeps_preceding_context(self):
        text = "The following operation is an unsafe example. Do not attempt it. Run PMP-104 above its approved pressure limit."
        windows = rerank_passages(text, "Run PMP-104 above its approved pressure limit?")
        self.assertNotIn("Run PMP-104 above its approved pressure limit.", windows)
        self.assertTrue(any("Do not attempt it." in window and "Run PMP-104" in window for window in windows))

    def test_hosted_reranker_scores_the_same_bounded_text_as_final_citations(self):
        async def check():
            submitted = []
            def response(request):
                payload = json.loads(request.content)
                submitted.extend(payload["documents"])
                return httpx.Response(200, json={"results": [
                    {"index": index, "relevance_score": .9 if index == 0 else .01}
                    for index in range(len(submitted))]})
            async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
                with patch("app.core.config.settings.cohere_api_key", SecretStr("test")):
                    ranked = await RerankService(client)._rerank_with_cohere("PMP-104 pressure guidance?", [
                        self.chunk("PMP-104 pressure guidance. " + "Background discussion. " * 70)])
            sources, _ = prepare_evidence("PMP-104 pressure guidance?", ranked)
            self.assertTrue(all(len(text) <= 900 for text in submitted))
            self.assertEqual(sources[0]["supporting_passage"], submitted[0])
        asyncio.run(check())


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
    def test_comparison_prototype_recognizes_only_explicit_two_part_syntax(self):
        from scripts.experiment_evidence_facets import comparison_facets
        self.assertEqual(comparison_facets("Contrast certificate checks with PDF permissions."),
                         ["certificate checks", "PDF permissions"])
        self.assertEqual(comparison_facets("How do certificate checks work?"), [])

    def test_comparison_prototype_refuses_when_one_part_lacks_evidence(self):
        from scripts.experiment_evidence_facets import merge_facet_sources
        source = {"document_id": "a", "document_name": "a.pdf", "page_number": 1,
                  "supporting_passage": "Literal text.", "source_id": "original"}
        self.assertEqual(merge_facet_sources([[source], []]), [])
        result = merge_facet_sources([[source], [source]])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["source_id"], "source-1")
        self.assertEqual(source["source_id"], "original")

    def test_context_challenge_labels_match_authored_source_text(self):
        root = Path(__file__).resolve().parents[1]
        challenge = load_dataset(root / "evaluation/questions-evidence-context.json")
        course = json.loads((root / "evaluation/course.json").read_text())
        pages = {(document["filename"], number): page["text"] for document in course["documents"]
                 for number, page in enumerate(document["pages"], 1)}
        from scripts.evaluate_retrieval_scaling import equipment_pdf
        import pymupdf
        for record in json.loads((root / "evaluation/questions-scaling.json").read_text())["documents"]:
            filename, content = equipment_pdf(record)
            with pymupdf.open(stream=content, filetype="pdf") as pdf:
                pages.update({(filename, number): " ".join(page.get_text().split())
                              for number, page in enumerate(pdf, 1)})
        for case in challenge["questions"]:
            self.assertIn(case["split"], ("development", "held_out"))
            if case["should_refuse"]:
                continue
            text = " ".join(pages[source["document_name"], source["page_number"]]
                            for source in case["expected_sources"])
            for fragment in case["expected_passage_fragments"]:
                with self.subTest(case=case["id"], fragment=fragment):
                    self.assertIn(fragment.casefold(), text.casefold())

    def test_source_loss_diagnostics_distinguish_pipeline_stages(self):
        from scripts.evaluate_retrieval_scaling import diagnose_source_losses
        expected = [{"document_name": "fixture.pdf", "page_number": page} for page in range(1, 6)]
        chunks = [{"metadata": source} for source in expected]
        losses = diagnose_source_losses(expected, chunks[1:], chunks[2:], chunks[3:], expected[4:])
        self.assertEqual([loss["stage"] for loss in losses],
                         ["search", "reranking", "evidence_gate", "citation_selection"])

    def test_source_loss_diagnostics_include_candidates_below_top_five(self):
        from scripts.evaluate_retrieval_scaling import diagnose_source_losses
        expected = [{"document_name": "second.pdf", "page_number": 1}]
        chunks = [{"metadata": {"document_name": "first.pdf", "page_number": i}}
                  for i in range(5)] + [{"metadata": expected[0]}]
        self.assertEqual(diagnose_source_losses(expected, chunks, chunks, chunks, expected), [])

    def test_scaling_pdf_fixtures_have_reproducible_bytes(self):
        from scripts.evaluate_retrieval_scaling import equipment_pdf
        record = {"id": "PMP-104", "pressure": "11.0", "interval": 60}
        self.assertEqual(equipment_pdf(record), equipment_pdf(record))

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
