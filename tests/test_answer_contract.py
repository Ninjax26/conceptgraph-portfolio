import asyncio
import copy
import unittest
from contextlib import ExitStack
from unittest.mock import patch
from pydantic import SecretStr

from app.services.synthesis_service import InvalidAnswerError, SynthesisService
from app.services.provider_failover import provider_circuit_breaker
from scripts.quality_metrics import audit_answer


SOURCES = [
    {"document_name": "manual.pdf", "page_number": 2, "supporting_passage": "Pressure limit: 11 bar."},
    {"document_name": "manual.pdf", "page_number": 7, "supporting_passage": "Inspect every 60 days."},
    {"document_name": "security.pdf", "page_number": None, "supporting_passage": "Authorize every document operation."},
]
REFUSAL = "I could not find enough reliable course content to answer this confidently."
QUESTION = "What is the pressure limit?"


class AnswerContractTests(unittest.TestCase):
    def test_invalid_indices_pages_and_malformed_labels_are_rejected(self):
        labels = ["[Source 9]", "[Source 0]", "[Source -1]", "[Source 1abc]", "[Source]",
                  "[Source 1, p. 7]", "[Source 3, p. 1]", "[Source 1, p. 0]",
                  "[Source 1, pp. 2]", "[Source 1", "[Source 1\n]", "[Source 1, p. two]",
                  "[Source " + "9" * 5000 + "]"]
        for label in labels:
            with self.subTest(label=label[:50]), self.assertRaises(InvalidAnswerError):
                SynthesisService._validate_answer("The limit is 11 bar. " + label, QUESTION, SOURCES)

    def test_reference_definitions_cannot_supply_visible_citation_or_answer_text(self):
        for answer in ["[Source 1]: https://example.com", "The limit is 11 bar.\n\n[Source 1]: https://example.com"]:
            with self.subTest(answer=answer), self.assertRaises(InvalidAnswerError):
                SynthesisService._validate_answer(answer, QUESTION, SOURCES)

    def test_one_valid_reference_cannot_hide_an_invalid_reference(self):
        with self.assertRaises(InvalidAnswerError):
            SynthesisService._validate_answer("Limit: 11 bar. [Source 1] [Source 9]", QUESTION, SOURCES)

    def test_missing_citations_and_absent_passages_are_rejected(self):
        for answer, sources in [("The limit is 11 bar.", SOURCES),
                                ("The limit is 11 bar. [Source 1]", []),
                                ("The limit is 11 bar. [Source 1]", [dict(SOURCES[0], supporting_passage=" ")])]:
            with self.subTest(answer=answer, sources=sources), self.assertRaises(InvalidAnswerError):
                SynthesisService._validate_answer(answer, QUESTION, sources)

    def test_citations_only_and_echo_with_citations_are_rejected(self):
        for answer in ["[Source 1]", "- [Source 1]\n- [Source 2]", "**What is the pressure limit?** [Source 1]", "WHAT IS THE PRESSURE LIMIT [Source 1, p. 2]"]:
            with self.subTest(answer=answer), self.assertRaises(InvalidAnswerError):
                SynthesisService._validate_answer(answer, QUESTION, SOURCES)

    def test_valid_answer_need_not_cite_redundant_sources(self):
        for answer in ["The limit is 11 bar. [Source 1]", "The limit is 11 bar. [source 1, p. 2]",
                       "The limit is 11 bar. [Source 1] Inspect every 60 days. [Source 2, p. 7]",
                       "[1, 2] is an example array. The limit is 11 bar. [Source 1]",
                       "What is the pressure limit? It is 11 bar. [Source 1]"]:
            self.assertEqual(SynthesisService._validate_answer(answer, QUESTION, SOURCES), answer)

    def test_unknown_page_requires_pageless_label(self):
        answer = "Authorize each document operation. [Source 3]"
        self.assertEqual(SynthesisService._validate_answer(answer, QUESTION, SOURCES), answer)

    def test_boolean_float_or_string_page_is_not_verified_page_provenance(self):
        for value in [True, 2.0, "2"]:
            with self.subTest(page=value), self.assertRaises(InvalidAnswerError):
                SynthesisService._validate_answer("Limit: 11 bar. [Source 1, p. 2]", QUESTION, [dict(SOURCES[0], page_number=value)])

    def test_exact_refusal_can_be_returned_without_citations_or_sources(self):
        self.assertEqual(SynthesisService._validate_answer(REFUSAL, QUESTION, []), REFUSAL)
        with self.assertRaises(InvalidAnswerError):
            SynthesisService._validate_answer(REFUSAL + " But the limit is 11 bar.", QUESTION, SOURCES)

    def test_offline_audit_detects_unfinished_reference_and_agrees_with_runtime(self):
        case = {"id": "fixture", "expected_refusal": False, "expected_claims": []}
        pages = {("manual.pdf", 2): SOURCES[0]["supporting_passage"]}
        result = audit_answer(case, {"answer": "Limit: 11 bar. [Source 1] [Source 2", "sources": SOURCES}, pages)
        self.assertEqual(result["invalid_citation_count"], 1)
        self.assertFalse(result["answer_contract_valid"])
        self.assertIsNone(result["groundedness"])
        self.assertIsNone(result["answer_completeness"])

    def test_offline_audit_marks_uncited_answer_as_structural_failure(self):
        case = {"id": "fixture", "expected_refusal": False, "expected_claims": []}
        result = audit_answer(case, {"answer": "Limit: 11 bar.", "sources": SOURCES}, {})
        self.assertFalse(result["answer_contract_valid"])
        self.assertEqual(result["citation_count"], 0)

    def test_prompt_requests_direct_cited_details_and_explicit_limits(self):
        prompt = SynthesisService._system_prompt()
        self.assertIn("direct answer", prompt)
        self.assertIn("missing", prompt)
        self.assertIn("source's listed page", prompt)
        self.assertIn("each factual", prompt)


class EvidenceFallbackTests(unittest.TestCase):
    def test_all_five_accepted_excerpts_are_retained_with_original_indices(self):
        sources = [dict(SOURCES[0], supporting_passage=f"Distinct evidence number {i}.", page_number=i) for i in range(1, 6)]
        saved = copy.deepcopy(sources)
        answer = SynthesisService._grounded_evidence_fallback(sources)
        for i in range(1, 6):
            self.assertIn(f"Distinct evidence number {i}.", answer)
            self.assertIn(f"[Source {i}, p. {i}]", answer)
        self.assertIn("not a generated answer", answer)
        self.assertIn("every requested detail", answer)
        self.assertEqual(sources, saved)

    def test_fallback_preserves_fact_near_end_of_accepted_900_character_passage(self):
        text = "context " * 104 + "input voltage is 24 V DC."
        self.assertLessEqual(len(text), 900)
        answer = SynthesisService._grounded_evidence_fallback([dict(SOURCES[0], supporting_passage=text)])
        self.assertIn("input voltage is 24 V DC.", answer)

    def test_oversized_passage_is_bounded_and_discloses_shortening(self):
        answer = SynthesisService._grounded_evidence_fallback([dict(SOURCES[0], supporting_passage="word " * 1000)])
        self.assertLess(len(answer), 1500)
        self.assertIn("shortened", answer)

    def test_blank_entries_preserve_reference_numbers_and_source_cap(self):
        sources = [dict(SOURCES[0], supporting_passage=""), SOURCES[1], SOURCES[2], SOURCES[1], SOURCES[2], dict(SOURCES[0], supporting_passage="Do not include sixth source.")]
        answer = SynthesisService._grounded_evidence_fallback(sources)
        self.assertNotIn("[Source 1", answer)
        self.assertIn("[Source 2, p. 7]", answer)
        self.assertIn("[Source 5]", answer)
        self.assertNotIn("sixth source", answer)
        self.assertIn("first 5", answer)

    def test_source_reference_text_is_escaped_not_promoted_to_answer_citation(self):
        from app.services.answer_contract import citation_references
        sources = [dict(SOURCES[0], supporting_passage="The manual prints [Source 99] and `example` literally.")]
        answer = SynthesisService._grounded_evidence_fallback(sources)
        refs = citation_references(answer, sources)
        self.assertEqual([ref["index"] for ref in refs], [1])
        self.assertIn(r"\[Source 99\]", answer)

    def test_fallback_keeps_gfm_controls_and_html_entities_literal(self):
        sources = [dict(SOURCES[0], supporting_passage="The limit is ~~11 bar~~ 9 bar; &#91;Source 99&#93; is printed literally.")]
        answer = SynthesisService._grounded_evidence_fallback(sources)
        self.assertIn(r"\~\~11 bar\~\~", answer)
        self.assertIn(r"\&#91;Source 99\&#93;", answer)

    def test_no_nonempty_source_returns_canonical_refusal(self):
        self.assertEqual(SynthesisService._grounded_evidence_fallback([]), REFUSAL)
        self.assertEqual(SynthesisService._grounded_evidence_fallback([{"supporting_passage": "  "}]), REFUSAL)


class AnswerProviderContractTests(unittest.TestCase):
    def setUp(self):
        provider_circuit_breaker.clear()
        self.addCleanup(provider_circuit_breaker.clear)

    def routes(self, provider, *, gemini=True):
        stack = ExitStack()
        stack.enter_context(patch("app.services.synthesis_service.settings.llm_provider", provider))
        for name, value in [("groq_api_key", "fixture"), ("gemini_api_key", "fixture" if gemini else None), ("cerebras_api_key", SecretStr("fixture"))]:
            stack.enter_context(patch("app.services.synthesis_service.settings." + name, value))
        async def observe(_provider, _operation, call, **kwargs):
            return call()
        stack.enter_context(patch("app.services.synthesis_service.observe_provider_call", side_effect=observe))
        return stack

    def test_invalid_groq_reference_uses_existing_backup_with_original_failure_type(self):
        service = SynthesisService()
        with self.routes("groq"), patch.object(service, "_synthesize_with_groq", return_value="Limit: 11 bar. [Source 9]") as primary, patch.object(service, "_synthesize_with_gemini", return_value="Limit: 11 bar. [Source 1, p. 2]") as backup:
            result = asyncio.run(service.synthesize_with_metadata(QUESTION, [], SOURCES))
        self.assertEqual(result.provider_used, "gemini")
        self.assertEqual(result.failover_reason, "InvalidAnswerError")
        self.assertTrue(result.failover_used)
        primary.assert_called_once()
        backup.assert_called_once()

    def test_invalid_direct_or_backup_provider_uses_existing_evidence_fallback(self):
        for provider, gemini in [("gemini", True), ("cerebras", False), ("groq", True), ("groq", False)]:
            provider_circuit_breaker.clear()
            service = SynthesisService()
            with self.subTest(provider=provider, gemini=gemini), self.routes(provider, gemini=gemini), patch.object(service, "_synthesize_with_groq", return_value="Uncited answer."), patch.object(service, "_synthesize_with_gemini", return_value="Wrong page. [Source 1, p. 99]"), patch.object(service, "_synthesize_with_cerebras", return_value="Wrong index. [Source 99]"):
                result = asyncio.run(service.synthesize_with_metadata(QUESTION, [], SOURCES))
            self.assertEqual(result.provider_used, "evidence_fallback")
            self.assertIn("[Source 3]", result.answer)

    def test_canonical_refusal_is_a_provider_answer_not_a_failed_generation(self):
        service = SynthesisService()
        with self.routes("groq"), patch.object(service, "_synthesize_with_groq", return_value=REFUSAL), patch.object(service, "_synthesize_with_gemini") as backup:
            result = asyncio.run(service.synthesize_with_metadata(QUESTION, [], SOURCES))
        self.assertEqual(result.answer, REFUSAL)
        self.assertEqual(result.provider_used, "groq")
        backup.assert_not_called()

    def test_cancellation_propagates_without_fallback_or_backup(self):
        service = SynthesisService()
        with self.routes("groq"), patch.object(service, "_synthesize_with_groq", side_effect=asyncio.CancelledError), patch.object(service, "_synthesize_with_gemini") as backup, self.assertRaises(asyncio.CancelledError):
            asyncio.run(service.synthesize_with_metadata(QUESTION, [], SOURCES))
        backup.assert_not_called()
