import asyncio
import copy
import unittest
from unittest.mock import patch

from app.services import answer_contract
from app.services.synthesis_service import SynthesisService, InvalidAnswerError
from scripts.quality_metrics import audit_answer
from tests import test_answer_contract as contract_tests

SOURCES=[{"document_name":"notes.pdf","page_number":3,"supporting_passage":"In this workflow, queues are a prerequisite for studying delivery retries. A successful enqueue does not guarantee processing completion."}]

class CitationScopeRevisionTests(unittest.TestCase):
    def test_typographic_labels_are_canonicalized_without_rewriting_claims(self):
        for tag,expected in [("【Source 1】","[Source 1]"),("【Source\u202f1, p.\u00a03】","[Source 1, p. 3]")]:
            text="The documented queue is optional. "+tag
            self.assertEqual(SynthesisService._validate_answer(text,"What is optional?",SOURCES),"The documented queue is optional. "+expected)

    def test_wrong_unfinished_or_mixed_typographic_labels_still_fail(self):
        for tag in ["【Source 2】","【Source 1, p. 9】","【Source -1】","【Source 1","【Source 1]","【Source 1, pp. 3】"]:
            with self.subTest(tag=tag),self.assertRaises(InvalidAnswerError):
                SynthesisService._validate_answer("Documented claim. [Source 1] "+tag,"Question?",SOURCES)

    def test_typographic_citation_only_echo_and_definitions_still_fail(self):
        for text in ["【Source 1】","Question? 【Source 1】","【Source 1】: https://example.test"]:
            with self.subTest(text=text),self.assertRaises(InvalidAnswerError):
                SynthesisService._validate_answer(text,"Question?",SOURCES)

    def test_escaped_typographic_labels_and_fallback_evidence_remain_literal(self):
        text=r"Literal \【Source 99】. Documented claim. [Source 1]"
        self.assertEqual(SynthesisService._validate_answer(text,"Question?",SOURCES),text)
        source=dict(SOURCES[0],supporting_passage="The document prints 【Source 99】 literally.")
        answer=SynthesisService._grounded_evidence_fallback([source])
        self.assertIn("&#12304;Source 99&#12305;",answer)
        self.assertEqual([x["index"] for x in answer_contract.citation_references(answer,[source])],[1])

    def test_offline_audit_agrees_on_raw_typographic_reference(self):
        result=audit_answer({"id":"x","question":"Question?","expected_refusal":False,"expected_claims":[]},
                            {"answer":"Documented claim. 【Source 1】","sources":SOURCES},
                            {("notes.pdf",3):SOURCES[0]["supporting_passage"]})
        self.assertTrue(result["answer_contract_valid"])
        self.assertEqual(result["citation_count"],1)
        self.assertIsNone(result["groundedness"])

    def test_source_constraints_are_literal_bounded_and_keep_original_indices(self):
        saved=copy.deepcopy(SOURCES)
        rows=SynthesisService._source_constraints("What does enqueue guarantee?",SOURCES)
        self.assertTrue(rows)
        self.assertTrue(any("does not guarantee" in row["quote"] for row in rows))
        self.assertTrue(any("In this workflow" in row["quote"] for row in rows))
        for row in rows:
            self.assertIn(row["quote"],SOURCES[row["source_index"]-1]["supporting_passage"])
        self.assertEqual(saved,SOURCES)
        prompt=SynthesisService._user_prompt("Question?",[],SOURCES)
        self.assertIn("Literal conditions to preserve",prompt)
        self.assertIn(SOURCES[0]["supporting_passage"],prompt)

    def test_constraint_selection_bounds_and_question_priority(self):
        source=dict(SOURCES[0],supporting_passage="A snapshot does not guarantee retention. "*10+"A receipt does not guarantee delivery.")
        rows=SynthesisService._source_constraints("Does a receipt guarantee delivery?",[source])
        self.assertLessEqual(len(rows),5)
        self.assertLessEqual(sum(len(row["quote"]) for row in rows),900)
        self.assertEqual(rows[0]["quote"],"A receipt does not guarantee delivery.")
        self.assertEqual(len({row["quote"] for row in rows}),len(rows))

    def test_prompt_requires_claim_check_without_specific_dataset_answers(self):
        prompt=SynthesisService._system_prompt()
        self.assertIn("Check each dependency or guarantee",prompt)
        self.assertIn("omit unsupported consequences",prompt)
        for word in ["qa06","sfh08","qdrant","readiness","transactions"]:
            self.assertNotIn(word,prompt.casefold())

    def test_typographic_primary_does_not_spend_backup_call(self):
        helper=contract_tests.AnswerProviderContractTests();service=SynthesisService()
        with helper.routes("groq"),patch.object(service,"_synthesize_with_groq",return_value="Documented claim. 【Source 1】"),patch.object(service,"_synthesize_with_gemini") as backup:
            result=asyncio.run(service.synthesize_with_metadata("Question?",[],SOURCES))
        self.assertEqual(result.answer,"Documented claim. [Source 1]")
        self.assertFalse(result.failover_used)
        backup.assert_not_called()

    def test_highlights_do_not_turn_truncated_sentences_into_complete_conditions(self):
        source=dict(SOURCES[0],supporting_passage="A receipt does not guarantee "+"delivery "*150)
        self.assertEqual(SynthesisService._source_constraints("receipt",[source]),[])

    def test_retrieval_bounded_fragments_are_not_highlighted_as_complete_conditions(self):
        from app.services.citation_service import build_sources
        text = "A receipt does not guarantee delivery " + "under all conditions " * 60
        sources = build_sources([{"text": text, "metadata": {"document_name": "notes.pdf", "page_number": 3}}])
        self.assertEqual(len(sources[0]["supporting_passage"]), 900)
        self.assertFalse(sources[0]["supporting_passage"].endswith("."))
        self.assertEqual(SynthesisService._source_constraints("receipt", sources), [])
        self.assertIn(sources[0]["supporting_passage"], SynthesisService._user_prompt("receipt", [], sources))
