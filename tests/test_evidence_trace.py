"""Offline diagnostics must explain real decisions without changing evidence."""
import copy
import json
import hashlib
from pathlib import Path
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.services.citation_service import prepare_evidence
from app.services.rerank_service import RerankService


def chunk(index, text, score=.8, rerank=5.):
    return dict(id=str(index), text=text, score=score, rerank_score=rerank,
                metadata=dict(upload_id=f"document-{index}", document_name="private-name.pdf", page_number=1))


class EvidenceTraceTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch("app.core.config.settings.evidence_min_score", .35))
        self.enterContext(patch("app.core.config.settings.retrieval_verify_identifiers", True))

    def traced(self, question, chunks):
        from app.services.evidence_trace import EvidenceTrace
        trace = EvidenceTrace()
        before = copy.deepcopy(chunks)
        ordinary = prepare_evidence(question, chunks)
        observed = prepare_evidence(question, chunks, trace=trace)
        self.assertEqual(observed, ordinary)
        self.assertEqual(chunks, before)
        # No raw source, identifier, document name or request text in diagnostic events.
        serialized = json.dumps(trace.events, allow_nan=False)
        self.assertNotIn("private-name", serialized)
        self.assertNotIn(question, serialized)
        return observed, trace.events

    def test_gate_explains_threshold_components_and_wrong_identifier(self):
        chunks = [chunk(1, "PMP-104 pressure is 11 bar."),
                  chunk(2, "Complementary pressure background.", .2, -8.),
                  chunk(3, "PMP-999 pressure is 20 bar.")]
        (sources, _), events = self.traced("What is the pressure of PMP-104?", chunks)
        self.assertEqual([s["document_id"] for s in sources], ["document-1"])
        gates = [e for e in events if e["stage"] == "evidence_gate"]
        self.assertEqual([e["reason"] for e in gates], ["accepted", "below_threshold", "other_identifier"])
        self.assertAlmostEqual(gates[1]["vector_component"], .06)
        self.assertLess(gates[1]["rerank_component"], .001)
        self.assertEqual(gates[1]["threshold"], .35)
        self.assertEqual(events[-1]["reason"], "sources_selected")

    def test_nonfinite_scores_remain_rejected_and_json_safe(self):
        # NaN cannot be compared to itself; check output equality directly here.
        from app.services.evidence_trace import EvidenceTrace
        trace = EvidenceTrace()
        chunks = [chunk(1, "Evidence", float("nan")), chunk(2, "Evidence", .8, float("inf"))]
        self.assertEqual(prepare_evidence("Describe evidence", chunks, trace=trace),
                         prepare_evidence("Describe evidence", chunks))
        self.assertEqual([e["reason"] for e in trace.events if e["stage"] == "evidence_gate"],
                         ["nonfinite_score", "nonfinite_score"])
        json.dumps(trace.events, allow_nan=False)

    def test_collective_and_final_identifier_loss_are_distinguished(self):
        (_, confidence), events = self.traced("What is PMP-104 pressure?", [chunk(1, "No equipment specification.")])
        self.assertEqual(confidence["level"], "insufficient")
        self.assertTrue(any(e["reason"] == "missing_accepted_identifiers" for e in events))
        # Identifier is present in full text, but the literal scored excerpt omits it.
        item = chunk(1, "PMP-104 equipment. Pressure is documented elsewhere.")
        item["rerank_passage"] = "Pressure is documented elsewhere."
        (sources, _), events = self.traced("What is PMP-104 pressure?", [item])
        self.assertEqual(sources, [])
        self.assertTrue(any(e["reason"] == "accepted" for e in events))
        self.assertEqual(events[-1]["reason"], "missing_cited_identifiers")

    def test_duplicate_and_citation_cap_explain_source_removal(self):
        items = [chunk(i, f"Background passage {i}.") for i in range(7)]
        items.insert(1, copy.deepcopy(items[0]))
        (sources, _), events = self.traced("Describe background passages", items)
        self.assertEqual(len(sources), 5)
        self.assertTrue(any(e["reason"] == "duplicate" for e in events))
        self.assertEqual(sum(e["reason"] == "citation_limit" for e in events), 2)
        selected = [e for e in events if e["stage"] == "final_source"]
        self.assertEqual(len(selected), 5)

    def test_comparison_missing_side_is_reported_without_admitting_it(self):
        item = dict(chunk(1, "DNS resolver evidence."), evidence_facet=0, evidence_facet_count=2)
        (sources, _), events = self.traced("Compare DNS with authentication", [item])
        self.assertEqual(sources, [])
        self.assertTrue(any(e["reason"] == "missing_comparison_side" and e["facet"] == 1 for e in events))
        self.assertEqual(events[-1]["reason"], "comparison_refused")

    def test_comparison_records_global_citation_cap(self):
        items = [dict(chunk(i, f"DNS authentication reference {i}."),
                      evidence_facet=i % 2, evidence_facet_count=2) for i in range(8)]
        (sources, _), events = self.traced("Compare DNS with authentication", items)
        self.assertEqual(len(sources), 5)
        self.assertEqual(sum(e["stage"] == "comparison_selection" and e["reason"] == "citation_limit"
                             for e in events), 3)

    def test_source_loss_details_link_actual_window_and_gate_decisions(self):
        from app.services.evidence_trace import EvidenceTrace
        from scripts.evaluate_retrieval_scaling import diagnose_source_losses
        item = chunk(1, "A low-scoring prerequisite passage.", .4, -6.)
        trace = EvidenceTrace()
        sources, _ = prepare_evidence("Explain prerequisites", [item], trace=trace)
        losses = diagnose_source_losses([dict(document_name="private-name.pdf", page_number=1)],
                                        [item], [item], [], sources, trace_events=trace.events)
        self.assertEqual(losses[0]["stage"], "evidence_gate")
        self.assertEqual(losses[0]["decisions"][0]["reason"], "below_threshold")
        self.assertLess(losses[0]["decisions"][0]["evidence_score"], .35)

    def test_traced_rerank_records_all_windows_and_preserves_first_tie(self):
        from app.services.evidence_trace import EvidenceTrace
        from scripts.evidence_selection_trace import TracingRerankService
        trace = EvidenceTrace()
        service = TracingRerankService(trace)
        chunks = [chunk(1, "First passage. Second passage."), chunk(2, "Third passage.")]
        windows = [(0, "First passage."), (0, "Second passage."), (1, "Third passage.")]
        scores = [3., 3., 2.]
        result = service._rank_windows(chunks, windows, iter(scores))
        self.assertEqual(result, RerankService._rank_windows(chunks, windows, scores))
        self.assertEqual(result[0]["rerank_passage"], "First passage.")
        self.assertEqual([e["reason"] for e in trace.events], ["selected_window", "lower_or_tied_window", "selected_window"])
        self.assertEqual(trace.events[1]["start"], 15)
        self.assertEqual(trace.events[1]["end"], 30)
        self.assertNotIn("First passage", json.dumps(trace.events))

    def test_raw_candidate_dropped_by_fusion_is_not_a_search_miss(self):
        from app.services.evidence_trace import EvidenceTrace
        from scripts.evaluate_retrieval_scaling import diagnose_source_losses
        required = chunk(1, "Required page returned by dense search.")
        other = chunk(2, "Another page selected by fusion.")
        other["metadata"]["document_name"] = "other.pdf"
        trace = EvidenceTrace()
        trace.record("dense_search", "candidate", required)
        trace.record("candidate_pool", "selected", other)
        losses = diagnose_source_losses([dict(document_name="private-name.pdf", page_number=1)],
                                        [other], [other], [], [], trace_events=trace.events)
        self.assertEqual(losses[0]["stage"], "candidate_fusion")
        self.assertEqual(losses[0]["decisions"][0]["stage"], "dense_search")
        self.assertEqual(losses[0]["decisions"][0]["reason"], "candidate")

    def test_unprocessed_comparison_side_is_attributed_to_coverage_refusal(self):
        from app.services.evidence_trace import EvidenceTrace
        from scripts.evaluate_retrieval_scaling import diagnose_source_losses
        item = dict(chunk(1, "Authentication establishes identity."), evidence_facet=1, evidence_facet_count=2)
        trace = EvidenceTrace()
        sources, _ = prepare_evidence("Compare DNS with authentication", [item], trace=trace)
        # The legacy reconstruction would accept this unprocessed second facet.
        losses = diagnose_source_losses([dict(document_name="private-name.pdf", page_number=1)],
                                        [item], [item], [item], sources, trace_events=trace.events)
        self.assertEqual(losses[0]["stage"], "comparison_coverage")
        self.assertEqual(losses[0]["blocking_decisions"][0]["reason"], "missing_comparison_side")
        self.assertFalse(any(e["stage"] == "evidence_gate" for e in trace.events))

    def test_identifier_limit_is_not_misattributed_to_a_later_gate(self):
        from app.services.evidence_trace import EvidenceTrace
        from scripts.evaluate_retrieval_scaling import diagnose_source_losses
        item = chunk(1, "Equipment details.")
        trace = EvidenceTrace()
        sources, _ = prepare_evidence("Describe " + " ".join(f"PMP-{i}" for i in range(9)), [item], trace=trace)
        losses = diagnose_source_losses([dict(document_name="private-name.pdf", page_number=1)],
                                        [item], [item], [item], sources, trace_events=trace.events)
        self.assertEqual(losses[0]["stage"], "identifier_guard")
        self.assertEqual(losses[0]["final_decision"]["reason"], "identifier_limit")
        self.assertFalse(any(e["stage"] == "evidence_gate" for e in trace.events))

    def test_four_known_losses_replay_real_window_selection_and_gate(self):
        from app.services.evidence_trace import EvidenceTrace
        from scripts.evidence_selection_trace import TracingRerankService
        fixture = json.loads((Path(__file__).parent / "fixtures/evidence-trace-regressions.json").read_text())
        for case in fixture["cases"]:
            with self.subTest(case=case["id"]):
                trace = EvidenceTrace()
                reranker = TracingRerankService(trace)
                reranker._model = SimpleNamespace(predict=lambda pairs: [
                    case["window_scores"][hashlib.sha256(passage.encode()).hexdigest()] for _, passage in pairs])
                ranked = reranker._rerank_locally(case["question"], [case["chunk"]])
                sources, _ = prepare_evidence(case["question"], ranked, trace=trace)
                self.assertEqual(sources, [])
                gate = next(e for e in trace.events if e["stage"] == "evidence_gate")
                self.assertEqual(gate["reason"], "below_threshold")
                self.assertAlmostEqual(gate["evidence_score"], case["expected_evidence_score"])

    def test_actual_vector_responses_record_dense_and_identifier_provenance(self):
        from app.services.evidence_trace import EvidenceTrace
        from scripts.evidence_selection_trace import TracingVectorClient
        from qdrant_client import QdrantClient
        from qdrant_client.models import Distance, PointStruct, VectorParams
        client = QdrantClient(":memory:")
        self.addCleanup(client.close)
        client.create_collection("fixture", vectors_config=VectorParams(size=2, distance=Distance.COSINE))
        client.upsert("fixture", points=[PointStruct(id=1, vector=[1., 0.], payload={
            "text": "private source text", "upload_id": "document-1", "page_number": 1})])
        trace = EvidenceTrace()
        observed = TracingVectorClient(client, trace)
        result = observed.query_points(collection_name="fixture", query=[1., 0.], limit=1, with_payload=True)
        points, cursor = observed.scroll(collection_name="fixture", limit=1, with_payload=True)
        self.assertEqual(result.points[0].id, points[0].id)
        self.assertIsNone(cursor)
        self.assertEqual([e["stage"] for e in trace.events], ["dense_search", "identifier_lookup"])
        self.assertEqual(trace.events[0]["chunk_key"], trace.events[1]["chunk_key"])
        self.assertNotIn("private source text", json.dumps(trace.events))


if __name__ == "__main__":
    unittest.main()
