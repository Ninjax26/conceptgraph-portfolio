import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException
from pydantic import ValidationError
from app.services.rag_service import RetrievalService
from app.api.endpoints import query
from scripts.run_evaluation import score_retrieval_result, summarize_retrieval


class RetrievalModesTests(unittest.IsolatedAsyncioTestCase):
    async def test_graph_errors_preserve_vector_evidence_and_report_actual_mode(self):
        for error in (RuntimeError("graph offline"), TimeoutError("graph slow")):
            service = RetrievalService(graph_driver=SimpleNamespace(), vector_client=SimpleNamespace())
            service.execute_graph_retrieval = AsyncMock(side_effect=error)
            service.search_qdrant = Mock(return_value=[{"text": "PDF evidence"}])
            context = SimpleNamespace(document_ids=["allowed"], graph_status="GRAPH_READY")
            result = await service.retrieve("Question", context, retrieval_mode="one_hop")
            self.assertEqual(result["chunks"], [{"text": "PDF evidence"}])
            self.assertEqual(result["graph_metadata"]["requested_mode"], "one_hop")
            self.assertEqual(result["graph_metadata"]["retrieval_mode"], "vector_only")
            service.search_qdrant.assert_called_once_with("Question", [], [], ["allowed"], 10)

    async def test_vector_failures_are_not_hidden_by_graph_fallback(self):
        service = RetrievalService(graph_driver=SimpleNamespace(), vector_client=SimpleNamespace())
        service.execute_graph_retrieval = AsyncMock(side_effect=RuntimeError("graph offline"))
        service.search_qdrant = Mock(side_effect=RuntimeError("vector offline"))
        with self.assertRaisesRegex(RuntimeError, "vector offline"):
            await service.retrieve("Question", SimpleNamespace(document_ids=["allowed"], graph_status="GRAPH_READY"))

    async def test_cancellation_does_not_start_fallback(self):
        service = RetrievalService(graph_driver=SimpleNamespace(), vector_client=SimpleNamespace())
        service.execute_graph_retrieval = AsyncMock(side_effect=asyncio.CancelledError())
        service.search_qdrant = Mock()
        with self.assertRaises(asyncio.CancelledError):
            await service.retrieve("Question", SimpleNamespace())
        service.search_qdrant.assert_not_called()

    async def test_request_passes_selected_mode_and_excludes_visualization_from_synthesis(self):
        context = SimpleNamespace()
        retrieval = SimpleNamespace(retrieve=AsyncMock(return_value={
            "chunks": [{"text": "Evidence"}], "graph_context": [{"concept": {"name": "Unrelated"}}],
            "graph_metadata": {"filter_reason": "course_graph_visualization_fallback"},
        }))
        synthesis = SimpleNamespace(synthesize=AsyncMock(return_value="Answer [Source 1]"))
        with patch.object(query.CourseService, "get_ready_context", AsyncMock(return_value=context)), \
             patch.object(query, "get_rerank_service", return_value=SimpleNamespace(rerank=AsyncMock(return_value=[]))), \
             patch.object(query, "assess_evidence", return_value=([], {"level": "high", "score": .9, "evidence_count": 1, "reason": "test"})), \
             patch.object(query, "build_sources", return_value=[{"source_id": "source-1"}]), \
             patch.object(query, "get_synthesis_service", return_value=synthesis):
            await query.query_conceptgraph(query.QueryRequest(question="Question", course_id="course", retrieval_mode="vector_only"), retrieval, None)
        self.assertEqual(retrieval.retrieve.await_args.kwargs["retrieval_mode"], "vector_only")
        self.assertEqual(synthesis.synthesize.await_args.kwargs["graph_context"], [])

    async def test_readiness_allows_optional_graph_outage_but_requires_vector_store(self):
        import app.main as main
        connection = AsyncMock()
        cm = AsyncMock()
        cm.__aenter__.return_value = connection
        with patch.object(main, "postgres_engine", SimpleNamespace(connect=Mock(return_value=cm))), \
             patch.object(main.neo4j_driver, "verify_connectivity", AsyncMock(side_effect=RuntimeError("offline"))), \
             patch.object(main.qdrant_client, "get_collections", return_value=[]), \
             patch.object(main.storage_service, "check_ready"), \
             patch.object(main, "processing_coordinator", SimpleNamespace(started=True, queue_depth=0)):
            ready = await main.readiness_check()
            self.assertEqual(ready["status"], "ready")
            self.assertEqual(ready["degraded_services"], ["neo4j"])
            with patch.object(main.qdrant_client, "get_collections", side_effect=RuntimeError("offline")):
                with self.assertRaises(HTTPException) as error:
                    await main.readiness_check()
                self.assertEqual(error.exception.status_code, 503)


class EvaluationIntegrityTests(unittest.TestCase):
    def test_invalid_retrieval_mode_is_rejected(self):
        with self.assertRaises(ValidationError):
            query.QueryRequest(question="Question", course_id="course", retrieval_mode="ten_hops")

    def test_traversal_aggregates_before_concatenating_paths(self):
        for hops in (1, 2):
            cypher = RetrievalService._fallback_cypher("HTTPS", max_hops=hops).cypher
            self.assertIn("WITH concept, prerequisite_paths,", cypher)
            self.assertIn("adjacent_nodes +", cypher)
            self.assertNotIn("collect(DISTINCT adjacent) +", cypher)

    def test_multisource_recall_distinguishes_retrieval_from_evidence_gate(self):
        case = {"question": "Compare", "expected_sources": [
            {"document_name": "a.pdf", "page_number": 1}, {"document_name": "b.pdf", "page_number": 2}]}
        ranked = [{"metadata": s} for s in case["expected_sources"]]
        result = score_retrieval_result(case, sources=case["expected_sources"][:1],
            confidence={}, graph_metadata={}, latency_seconds=1, ranked_chunks=ranked, candidates=ranked)
        self.assertTrue(result["reranked_retrieval"]["all_sources_hit"])
        self.assertFalse(result["accepted_evidence"]["all_sources_hit"])
        self.assertEqual(result["accepted_evidence"]["source_recall"], .5)

    def test_error_is_not_a_successful_refusal(self):
        result = score_retrieval_result({"question": "Unsupported", "should_refuse": True}, sources=[],
            confidence={"level": "insufficient"}, graph_metadata={}, latency_seconds=1, error="Timeout")
        self.assertFalse(result["refused"])
        self.assertEqual(summarize_retrieval([result])["request_errors"], 1)

    def test_public_sources_and_evaluation_labels_exist_in_pdf_text(self):
        import pymupdf as fitz
        root = Path(__file__).resolve().parents[1]
        course = json.loads((root / "evaluation/course.json").read_text())
        public = json.loads((root / "public/sample/course.json").read_text())
        self.assertEqual(course, public)
        pages = {}
        for document in course["documents"]:
            with fitz.open(root / "public/sample" / document["filename"]) as pdf:
                self.assertEqual(len(pdf), len(document["pages"]))
                for i, page in enumerate(document["pages"]):
                    text = " ".join(pdf[i].get_text().split())
                    self.assertIn(" ".join(page["text"].split()), text)
                    pages[(document["filename"], i+1)] = text
        questions = json.loads((root / "evaluation/questions-multidoc.json").read_text())["questions"]
        for question in questions:
            for source in question["expected_sources"]:
                self.assertIn((source["document_name"], source["page_number"]), pages)
        for example in public["saved_examples"]:
            for source in example["sources"]:
                self.assertIn(tuple(source), pages)
