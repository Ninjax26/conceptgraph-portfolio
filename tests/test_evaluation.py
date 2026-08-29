import json
import tempfile
import unittest
from pathlib import Path

from scripts.run_evaluation import REFUSAL_TEXT, load_dataset, score_response, summarize


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


if __name__ == "__main__":
    unittest.main()
