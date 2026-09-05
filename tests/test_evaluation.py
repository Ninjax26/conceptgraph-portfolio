import json
import tempfile
import unittest
from pathlib import Path

from scripts.run_evaluation import (
    REFUSAL_TEXT,
    ablation_markdown_table,
    build_ablation_comparison,
    load_dataset,
    score_response,
    score_retrieval_result,
    summarize,
    summarize_retrieval,
)


class SmallEvaluationTests(unittest.TestCase):
    def test_dataset_requires_expected_source_for_supported_questions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "questions.json"
            path.write_text(
                json.dumps(
                    {
                        "questions": [
                            {"question": "Supported?", "should_refuse": False}
                        ]
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "expected_document"):
                load_dataset(path)

    def test_response_scoring_uses_only_top_five_sources(self):
        case = {
            "question": "What is quishing?",
            "expected_document": "Cyber.pdf",
            "expected_page": 4,
            "should_refuse": False,
        }
        response = {
            "answer": "Quishing uses QR codes. [Source 1]",
            "sources": [
                {"document_name": "Other.pdf", "page_number": index}
                for index in range(1, 6)
            ]
            + [{"document_name": "Cyber.pdf", "page_number": 4}],
            "confidence": {"level": "high"},
        }

        result = score_response(case, response, 1.25)

        self.assertFalse(result["document_correct"])
        self.assertFalse(result["page_correct"])
        self.assertTrue(result["citation_present"])

    def test_refusal_requires_no_sources_and_insufficient_confidence(self):
        case = {"question": "Outside scope?", "should_refuse": True}
        response = {
            "answer": REFUSAL_TEXT,
            "sources": [],
            "confidence": {"level": "insufficient"},
        }

        result = score_response(case, response, 0.5)
        summary = summarize([result])

        self.assertTrue(result["refused"])
        self.assertEqual(summary["unsupported_questions_refused"], 1)
        self.assertEqual(summary["unsupported_question_count"], 1)

    def test_retrieval_ablation_scores_sources_without_answer_generation(self):
        case = {
            "question": "What is quishing?",
            "expected_document": "Cyber.pdf",
            "expected_page": 4,
            "should_refuse": False,
        }

        result = score_retrieval_result(
            case,
            sources=[{"document_name": "Cyber.pdf", "page_number": 4}],
            confidence={"level": "high"},
            graph_metadata={
                "retrieval_mode": "one_hop",
                "graph_expansion": {
                    "anchor_match_found": True,
                    "one_hop_count": 2,
                    "two_hop_count": 0,
                },
            },
            latency_seconds=0.25,
        )

        self.assertTrue(result["document_correct"])
        self.assertTrue(result["page_correct"])
        self.assertTrue(result["evidence_found"])
        self.assertEqual(result["retrieval_mode"], "one_hop")
        self.assertEqual(result["one_hop_terms"], 2)

    def test_retrieval_summary_and_comparison_use_vector_only_as_baseline(self):
        supported = {
            "question": "Supported?",
            "retrieval_mode": "vector_only",
            "should_refuse": False,
            "document_correct": True,
            "page_correct": False,
            "evidence_found": True,
            "refused": False,
            "latency_seconds": 1.0,
            "graph_anchor_match_found": False,
            "error": None,
        }
        unsupported = {
            **supported,
            "question": "Unsupported?",
            "should_refuse": True,
            "document_correct": None,
            "page_correct": None,
            "evidence_found": False,
            "refused": True,
            "latency_seconds": 2.0,
        }
        vector_summary = summarize_retrieval([supported, unsupported])
        one_hop_summary = {
            **vector_summary,
            "correct_source_page_top_5": 1,
            "average_retrieval_seconds": 1.75,
        }
        two_hop_summary = {
            **one_hop_summary,
            "average_retrieval_seconds": 2.0,
        }
        runs = {
            "vector_only": {"summary": vector_summary, "results": []},
            "one_hop": {"summary": one_hop_summary, "results": []},
            "two_hop": {"summary": two_hop_summary, "results": []},
        }

        comparison = build_ablation_comparison(runs)
        table = ablation_markdown_table(runs)

        self.assertEqual(comparison["one_hop"]["page_hits_delta"], 1)
        self.assertEqual(
            comparison["one_hop"]["average_latency_delta_seconds"], 0.25
        )
        self.assertIn("| Metric | Vector only | One hop | Two hop |", table)
        self.assertIn("| Primary expected page in top 5 (single-page label) | 0/1 | 1/1 | 1/1 |", table)


if __name__ == "__main__":
    unittest.main()
