import copy
import json
from pathlib import Path
import tempfile
import unittest

from scripts import prepare_answer_comparison as pack
from app.services.synthesis_service import SynthesisService


class AnswerComparisonPackTests(unittest.TestCase):
    def fixtures(self):
        cases = [{"id": "a", "question": "What is supported?", "sources": [["notes.pdf", 1]],
                  "expected_refusal": False, "expected_claims": [{"id": "secret-checklist", "text": "Never send this checklist"}]},
                 {"id": "b", "question": "No evidence?", "sources": [], "expected_refusal": True, "expected_claims": []}]
        pages = {("notes.pdf", 1): "Only this source evidence."}
        sources = [{"source_id": "source-1", "document_id": "quality-notes.pdf", "document_name": "notes.pdf",
                    "page_number": 1, "supporting_passage": pages["notes.pdf", 1]}]
        baseline = [{"id": "a", "question": cases[0]["question"], "response": {"answer": "Old answer", "sources": sources}},
                    {"id": "b", "question": cases[1]["question"], "response": {"answer": "Old refusal", "sources": []}}]
        return cases, pages, baseline

    def test_known_requests_keep_exact_context_and_exclude_answers_and_checklists(self):
        cases,pages,baseline=self.fixtures()
        result=pack.build_cases(cases,pages,baseline=baseline)
        self.assertEqual(result[0]["sources"],baseline[0]["response"]["sources"])
        payload=json.dumps(result[0]["request"])
        self.assertNotIn("Old answer",payload)
        self.assertNotIn("Never send this checklist",payload)
        self.assertEqual(result[0]["request"]["messages"][0]["content"],SynthesisService._system_prompt())
        self.assertEqual(result[1]["action"],"local_refusal_no_request")
        self.assertNotIn("request",result[1])

    def test_rejects_baseline_mismatch_duplicate_missing_or_changed_passage(self):
        cases,pages,baseline=self.fixtures()
        variants=[baseline[:1],baseline+[baseline[0]]]
        changed=copy.deepcopy(baseline);changed[0]["response"]["sources"][0]["supporting_passage"]="Invented text"
        variants.append(changed)
        changed=copy.deepcopy(baseline);changed[0]["response"]["sources"][0]["page_number"]=2
        variants.append(changed)
        changed=copy.deepcopy(baseline);changed[0]["question"]="Different input with the same ID"
        variants.append(changed)
        for records in variants:
            with self.subTest(records=records),self.assertRaises(ValueError):
                pack.build_cases(cases,pages,baseline=records)

    def test_rejects_missing_page_and_oversized_context_without_silent_truncation(self):
        cases,pages,_=self.fixtures()
        with self.assertRaises(ValueError):pack.build_cases(cases,{})
        with self.assertRaises(ValueError):pack.build_cases(cases,{("notes.pdf",1):"x"*901})
        cases[0]["sources"]=[["notes.pdf",1]]*6
        with self.assertRaises(ValueError):pack.build_cases(cases,pages)

    def test_budget_counts_requests_once_and_hashes_exact_payload(self):
        cases,pages,baseline=self.fixtures()
        rows=pack.build_cases(cases,pages,baseline=baseline)
        result=pack.summarize(rows)
        self.assertEqual(result["maximum_hosted_requests"],1)
        self.assertEqual(result["maximum_completion_tokens_total"],1200)
        self.assertEqual(result["prompt_characters"],sum(len(m["content"]) for m in rows[0]["request"]["messages"]))
        self.assertEqual(rows[0]["request_sha256"],pack.digest(rows[0]["request"]))
        self.assertEqual(rows[0]["request"]["max_completion_tokens"],1200)

    def test_artifact_writer_never_overwrites_prior_capture_or_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/"pack.json"
            pack.write_new(path,{"state":"first"})
            with self.assertRaises(FileExistsError):pack.write_new(path,{"state":"second"})
            self.assertEqual(json.loads(path.read_text()),{"state":"first"})
