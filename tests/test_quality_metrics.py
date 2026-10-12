import hashlib
import json
import unittest

from scripts.quality_metrics import audit_answer, audit_graph, audit_graph_reviews, content_hash, review_context_hash
from scripts.run_evaluation import REFUSAL_TEXT


class QualityMetricTests(unittest.TestCase):
    def setUp(self):
        self.case = {"id": "q", "expected_refusal": False,
                     "expected_claims": [{"id": "limit", "text": "Pressure limit is 11 bar."},
                                         {"id": "interval", "text": "Inspect every 60 days."}]}
        self.pages = {("manual.pdf", 1): "PMP-104 pressure limit is 11 bar. Inspect every 60 days."}
        self.response = {"answer": "The limit is 11 bar. [Source 1, p. 1]", "sources": [
            {"document_name": "manual.pdf", "page_number": 1, "supporting_passage": "PMP-104 pressure limit is 11 bar."}]}

    def review(self):
        return {"reviewer": "test reviewer", "content_sha256": content_hash(self.response),
                "review_context_sha256": review_context_hash(self.case, self.response, self.pages),
                "all_claims_reviewed": True, "refusal_correct": False,
                "expected_claims_covered": ["limit"], "claims": [
                    {"text": "The limit is 11 bar.", "source_indices": [1], "supported": True, "citations_correct": True}]}

    def test_citation_presence_is_not_groundedness_or_semantic_correctness(self):
        result = audit_answer(self.case, self.response, self.pages)
        self.assertEqual(result["invalid_citation_count"], 0)
        self.assertIsNone(result["groundedness"])
        self.assertIsNone(result["citation_correctness"])
        self.assertIsNone(result["answer_completeness"])

    def test_unknown_zero_and_malformed_citations_do_not_pass(self):
        for label in ("[Source 99]", "[Source 0]", "[Source -1]", "[Source 1abc]"):
            result = audit_answer(self.case, dict(self.response, answer=f"A fact. {label}"), self.pages)
            self.assertEqual(result["invalid_citation_count"], 1)
            self.assertFalse(result["cited_sources_literal"])

    def test_wrong_page_is_detected_even_when_source_index_exists(self):
        result = audit_answer(self.case, dict(self.response, answer="Fact. [Source 1, p. 7]"), self.pages)
        self.assertEqual(result["invalid_citation_count"], 0)
        self.assertEqual(result["wrong_cited_page_count"], 1)

    def test_altered_excerpt_does_not_count_as_literal_source(self):
        bad = {"answer": "99 bar. [Source 1]", "sources": [dict(self.response["sources"][0], supporting_passage="PMP-104 pressure limit is 99 bar.")]}
        result = audit_answer(self.case, bad, self.pages)
        self.assertEqual(result["literal_source_count"], 0)
        self.assertFalse(result["cited_sources_literal"])

    def test_hash_bound_human_review_scores_claims_and_completeness_separately(self):
        result = audit_answer(self.case, self.response, self.pages, self.review())
        self.assertEqual(result["groundedness"], 1.)
        self.assertEqual(result["citation_correctness"], 1.)
        self.assertEqual(result["answer_completeness"], .5)

    def test_reviews_become_stale_when_answer_or_sources_change(self):
        for response in (dict(self.response, answer="Changed. [Source 1]"),
                         dict(self.response, sources=[dict(self.response["sources"][0], supporting_passage="Inspect every 60 days.")])):
            result = audit_answer(self.case, response, self.pages, self.review())
            self.assertEqual(result["review_status"], "invalid_or_stale")
            self.assertIsNone(result["groundedness"])

    def test_unreviewed_extra_claims_cannot_receive_semantic_score(self):
        review = dict(self.review(), all_claims_reviewed=False)
        self.assertIsNone(audit_answer(self.case, self.response, self.pages, review)["groundedness"])

    def test_refusal_can_be_correct_even_with_relevant_nonanswering_sources(self):
        case = dict(self.case, expected_refusal=True, expected_claims=[])
        response = dict(self.response, answer=REFUSAL_TEXT)
        review = dict(self.review(), content_sha256=content_hash(response), claims=[], expected_claims_covered=[], refusal_correct=True)
        review["review_context_sha256"] = review_context_hash(case, response, self.pages)
        result = audit_answer(case, response, self.pages, review)
        self.assertTrue(result["refusal_correct"])

    def graph(self):
        return {"nodes": [{"id": "a", "name": "HTTP", "document_name": "manual.pdf", "page_number": 1,
                           "upload_id": "fixture", "source_chunk_id": "fixture:1:0"},
                          {"id": "b", "name": "TLS", "document_name": "manual.pdf", "page_number": 1,
                           "upload_id": "fixture", "source_chunk_id": "fixture:1:0"}],
                "relationships": [{"source_node_id": "a", "target_node_id": "b", "relation_type": "PREREQUISITE_OF"}]}

    def reference(self):
        return [{"document_name": "manual.pdf", "source_names": ["HTTP"], "target_names": ["TLS"],
                 "relation_type": "PREREQUISITE_OF"}]

    def test_reversed_prerequisite_is_not_a_correct_direction_match(self):
        graph = self.graph()
        graph["relationships"][0].update(source_node_id="b", target_node_id="a")
        result = audit_graph("manual.pdf", graph, self.reference(), self.pages)
        self.assertEqual(result["reference_edges_found"], 0)
        self.assertEqual(result["known_reversed_edge_indices"], [0])

    def test_related_to_is_symmetric_but_duplicate_edges_are_detected(self):
        graph = self.graph()
        graph["relationships"] = [{"source_node_id": "a", "target_node_id": "b", "relation_type": "RELATED_TO"},
                                  {"source_node_id": "b", "target_node_id": "a", "relation_type": "RELATED_TO"}]
        result = audit_graph("manual.pdf", graph, [], self.pages)
        self.assertEqual(result["duplicate_edges"], 1)
        self.assertEqual(result["known_reversed_edge_indices"], [])

    def test_raw_dangling_and_unsupported_edges_are_audited_before_cleanup(self):
        graph = self.graph()
        graph["relationships"].append({"source_node_id": "missing", "target_node_id": "a", "relation_type": "INVENTED"})
        result = audit_graph("manual.pdf", graph, [], self.pages)
        self.assertEqual(result["dangling_edges"], 1)
        self.assertEqual(result["unsupported_types"], 1)
        self.assertIsNone(result["semantic_precision"])

    def test_partial_reference_does_not_call_unlabelled_edge_wrong(self):
        result = audit_graph("manual.pdf", self.graph(), [], self.pages)
        self.assertEqual(result["unreviewed_edge_indices"], [0])
        self.assertIsNone(result["semantic_precision"])

    def test_graph_review_requires_hash_and_literal_quote(self):
        graph = self.graph()
        digest = hashlib.sha256(json.dumps(graph, sort_keys=True).encode()).hexdigest()
        review = {"document_name": "manual.pdf", "edge_index": 0, "snapshot_sha256": digest,
                  "reviewer": "test reviewer", "supported": True, "direction_correct": True,
                  "supporting_evidence": [{"document_name": "manual.pdf", "page_number": 1, "quote": "Inspect every 60 days."}]}
        self.assertEqual(audit_graph_reviews("manual.pdf", graph, self.pages, [review])["human_reviewed_edges"], 1)
        review["snapshot_sha256"] = "stale"
        result = audit_graph_reviews("manual.pdf", graph, self.pages, [review])
        self.assertEqual(result["human_reviewed_edges"], 0)
        self.assertIsNone(result["semantic_precision"])

    def test_positive_citation_review_cannot_override_a_fabricated_excerpt(self):
        response = dict(self.response, sources=[dict(self.response["sources"][0], supporting_passage="99 bar.")])
        review = dict(self.review(), content_sha256=content_hash(response))
        review["review_context_sha256"] = review_context_hash(self.case, response, self.pages)
        result = audit_answer(self.case, response, self.pages, review)
        self.assertEqual(result["review_status"], "invalid_or_stale")
        self.assertIsNone(result["citation_correctness"])

    def test_review_becomes_stale_when_source_context_changes_beyond_excerpt(self):
        pages = {("manual.pdf", 1): self.pages["manual.pdf", 1] + " Later revision changes the interval."}
        result = audit_answer(self.case, self.response, pages, self.review())
        self.assertEqual(result["review_status"], "invalid_or_stale")
        self.assertIsNone(result["groundedness"])

    def test_all_six_types_are_allowed_structurally_not_assumed_semantically(self):
        from app.schemas.extraction import ALLOWED_RELATIONSHIP_TYPES
        graph = self.graph()
        graph["relationships"] = [{"source_node_id": "a", "target_node_id": "b", "relation_type": kind}
                                  for kind in ALLOWED_RELATIONSHIP_TYPES]
        result = audit_graph("manual.pdf", graph, [], self.pages)
        self.assertEqual(result["unsupported_types"], 0)
        self.assertEqual(len(result["unreviewed_edge_indices"]), 6)
