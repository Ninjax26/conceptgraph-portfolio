import asyncio
from contextlib import ExitStack, redirect_stdout
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx
from pydantic import SecretStr
from app.services.gemini_service import GeminiService
from app.core.exceptions import LLMProviderUnavailableError
from app.services.provider_failover import provider_circuit_breaker
from app.services.synthesis_service import SynthesisService
from scripts import evaluate_quality


SOURCE = {"document_name": "sample_notes.pdf", "page_number": 4,
          "section_heading": "Scope and limits", "supporting_passage": "The source says this only in this workflow."}


class SourceFaithfulSynthesisTests(unittest.TestCase):
    def test_input_estimate_counts_exact_assembled_system_and_user_text(self):
        question = "What does this establish?"
        graph = [{"description": "A related concept."}]
        sources = [dict(SOURCE, document_name="résumé notes.pdf")]
        actual = SynthesisService()._prompt_character_count(question, graph, sources)
        expected = len(SynthesisService._system_prompt()) + len(SynthesisService._user_prompt(question, graph, sources))
        self.assertEqual(actual, expected)

    def test_input_estimate_follows_class_prompt_overrides(self):
        class CustomPrompt(SynthesisService):
            @staticmethod
            def _system_prompt():return "local system text"
            @staticmethod
            def _user_prompt(question,graph_context,sources):return "local user text"
        self.assertEqual(CustomPrompt()._prompt_character_count("Question?",[],[SOURCE]),len("local system text")+len("local user text"))

    def test_input_estimate_uses_bounded_graph_not_discarded_graph_tail(self):
        graph = [{"description": "context " * 10000}]
        expected = len(SynthesisService._system_prompt()) + len(SynthesisService._user_prompt("Question?", graph, [SOURCE]))
        self.assertEqual(SynthesisService()._prompt_character_count("Question?", graph, [SOURCE]), expected)
        self.assertLess(expected, SynthesisService.MAX_GRAPH_CONTEXT_CHARS + 5000)

    def test_input_estimate_includes_source_metadata_even_if_passage_is_unchanged(self):
        short = [SOURCE]
        longer = [dict(SOURCE, document_name="long source name " * 20, section_heading="section heading " * 20)]
        short_count = SynthesisService()._prompt_character_count("Question?", [], short)
        long_count = SynthesisService()._prompt_character_count("Question?", [], longer)
        self.assertGreater(long_count, short_count)
        self.assertEqual(long_count-short_count, len(SynthesisService._user_prompt("Question?", [], longer))-len(SynthesisService._user_prompt("Question?", [], short)))

    def test_generation_contract_covers_observed_failure_classes_generically(self):
        prompt = SynthesisService._system_prompt().casefold()
        for rule in ["examples only", "qualifiers", "all parts", "material limitations", "same fact once"]:
            with self.subTest(rule=rule):
                self.assertIn(rule, prompt)
        for diagnostic_answer in ["pmp-104", "11.0 bar", "60 days", "postgresql", "qdrant", "qa08", "qa01"]:
            self.assertNotIn(diagnostic_answer, prompt)

    def test_refusal_rule_unambiguously_preserves_partial_answers(self):
        self.assertIn("none of the requested parts", SynthesisService._system_prompt().casefold())

    def test_instance_prompt_overrides_match_actual_dispatch(self):
        class CustomPrompt(SynthesisService):
            def _system_prompt(self):return "instance system"
            def _user_prompt(self,question,graph_context,sources):return "instance user"
        service=CustomPrompt()
        try:count=service._prompt_character_count("Question?",[],[SOURCE])
        except TypeError:self.fail("Instance prompt overrides must be metered through the instance")
        self.assertEqual(count,len(service._system_prompt())+len(service._user_prompt("Question?",[],[SOURCE])))

    def test_gemini_counter_matches_mocked_wire_prompt_text(self):
        service=SynthesisService();observed=[]
        def handler(request):
            payload=json.loads(request.content)
            observed.append(payload["contents"][0]["parts"][0]["text"])
            return httpx.Response(200,json={"candidates":[{"content":{"parts":[{"text":"Supported answer. [Source 1]"}]}}]})
        with httpx.Client(base_url="https://example.test",transport=httpx.MockTransport(handler)) as client,patch("app.services.gemini_service.settings.gemini_api_key","fixture-only"):
            GeminiService(client).generate([service._system_prompt(),service._user_prompt("Question?",[],[SOURCE])])
        try:count=service._prompt_character_count("Question?",[],[SOURCE],provider="gemini")
        except TypeError:self.fail("Prompt accounting must include provider-specific text assembly")
        self.assertEqual(count,len(observed[0]))

    def test_source_order_literal_text_graph_bound_and_inputs_are_preserved(self):
        sources = [SOURCE, dict(SOURCE, document_name="next.pdf", supporting_passage="Do not convert an optional condition into a guarantee.")]
        graph = [{"description": "graph " * 10000}]
        saved = copy.deepcopy((graph, sources))
        prompt = SynthesisService._user_prompt("Question?", graph, sources)
        self.assertIn(SOURCE["supporting_passage"], prompt)
        self.assertIn(sources[1]["supporting_passage"], prompt)
        self.assertLess(prompt.index("[Source 1]"), prompt.index("[Source 2]"))
        self.assertLess(len(prompt), SynthesisService.MAX_GRAPH_CONTEXT_CHARS + 1500)
        self.assertEqual((graph, sources), saved)

    def test_every_provider_route_reserves_its_assembled_prompt_size(self):
        routes = [("groq", True, False), ("gemini", True, False), ("cerebras", False, False),
                  ("groq", True, True), ("groq", False, True)]
        self.addCleanup(provider_circuit_breaker.clear)
        for provider, gemini, primary_fails in routes:
            provider_circuit_breaker.clear()
            observed = []
            service = SynthesisService()
            async def observe(name, operation, generate, **kwargs):
                observed.append((name, kwargs["input_characters"]))
                return generate()
            with self.subTest(provider=provider, gemini=gemini, primary_fails=primary_fails), ExitStack() as stack:
                for key,value in [("llm_provider",provider),("groq_api_key","fixture-only"),
                                  ("gemini_api_key","fixture-only" if gemini else None),("cerebras_api_key",SecretStr("fixture-only"))]:
                    stack.enter_context(patch("app.services.synthesis_service.settings."+key,value))
                stack.enter_context(patch("app.services.synthesis_service.observe_provider_call",side_effect=observe))
                primary = stack.enter_context(patch.object(service,"_synthesize_with_groq",return_value="An answer. [Source 1]"))
                if primary_fails:primary.side_effect=LLMProviderUnavailableError("fixture unavailable")
                backup_g = stack.enter_context(patch.object(service,"_synthesize_with_gemini",return_value="An answer. [Source 1]"))
                backup_c = stack.enter_context(patch.object(service,"_synthesize_with_cerebras",return_value="An answer. [Source 1]"))
                result = asyncio.run(service.synthesize_with_metadata("Question?",[],[SOURCE]))
                expected = len(service._system_prompt()) + len(service._user_prompt("Question?",[],[SOURCE]))
                self.assertEqual(len(observed),2 if primary_fails else 1)
                self.assertTrue(all(count==expected+(2 if name=="gemini" else 0) for name,count in observed))
                self.assertNotEqual(result.provider_used,"evidence_fallback")
                if not primary_fails:
                    self.assertFalse(result.failover_used)
                    if provider=="groq":backup_g.assert_not_called();backup_c.assert_not_called()


class QualityCaseSelectionTests(unittest.TestCase):
    def make_args(self, directory, *, custom=True, unknown=False, relative=False):
        directory = Path(directory)
        cases = directory/"custom-cases.json"
        case = {"id":"temporary-case", "question":"Which fields are listed?", "sources":[["not-a-fixture.pdf" if unknown else "web-foundations.pdf",1]],
                "expected_refusal":False,"expected_claims":[{"id":"fields","text":"Request and response fields are listed."}]}
        cases.write_text(json.dumps({"name":"Temporary authored test cases", "cases":[case]}))
        graph = directory/"empty-graph.json";graph.write_text('{}')
        selected = Path(os.path.relpath(cases,evaluate_quality.ROOT)) if relative else cases
        return SimpleNamespace(cases=selected if custom else None, fallback=True,responses=None,reviews=None,graph_snapshot=graph,output=directory/"result"),cases

    def test_custom_cases_are_used_and_hash_bound_instead_of_default(self):
        with tempfile.TemporaryDirectory() as directory:
            args,cases = self.make_args(directory)
            with redirect_stdout(io.StringIO()):evaluate_quality.run(args)
            result = json.loads(args.output.with_suffix('.json').read_text())
            self.assertEqual(result['dataset'],'Temporary authored test cases')
            self.assertEqual(result['summary']['expected_cases'],1)
            self.assertEqual(result['summary']['captured_answers'],1)
            self.assertEqual(result['dataset_sha256'],hashlib.sha256(cases.read_bytes()).hexdigest())
            self.assertEqual(result['provider_calls'],0)
            self.assertEqual(result['summary']['reviewed_answers'],0)

    def test_relative_case_path_is_resolved_against_project_root(self):
        with tempfile.TemporaryDirectory() as directory:
            args,_ = self.make_args(directory,relative=True)
            with redirect_stdout(io.StringIO()):evaluate_quality.run(args)
            result = json.loads(args.output.with_suffix('.json').read_text())
            self.assertEqual(result['summary']['expected_cases'],1)

    def test_custom_case_with_unknown_source_fails_before_capture(self):
        with tempfile.TemporaryDirectory() as directory:
            args,_ = self.make_args(directory,unknown=True)
            with self.assertRaisesRegex(ValueError,'missing authored source page'):
                with redirect_stdout(io.StringIO()):evaluate_quality.run(args)
            self.assertFalse(args.output.with_suffix('.json').exists())

    def test_required_case_fields_and_source_page_types_are_validated(self):
        changes=[lambda c:c.pop("question"),lambda c:c.pop("expected_refusal"),lambda c:c.pop("expected_claims"),
                 lambda c:c.update(question=42),lambda c:c.update(expected_refusal="false"),
                 lambda c:c.update(sources=[["web-foundations.pdf",True]]),lambda c:c.update(sources=[["web-foundations.pdf",1.0]]),
                 lambda c:c.update(expected_claims="claims")]
        for change in changes:
            with self.subTest(change=change),tempfile.TemporaryDirectory() as directory:
                args,cases=self.make_args(directory)
                data=json.loads(cases.read_text());change(data["cases"][0]);cases.write_text(json.dumps(data))
                with self.assertRaises(ValueError):
                    with redirect_stdout(io.StringIO()):evaluate_quality.run(args)
                self.assertFalse(args.output.with_suffix('.json').exists())

    def test_duplicate_expected_claim_ids_cannot_inflate_completeness(self):
        with tempfile.TemporaryDirectory() as directory:
            args,cases=self.make_args(directory)
            data=json.loads(cases.read_text());data["cases"][0]["expected_claims"]=[{"id":"same","text":"First fact"},{"id":"same","text":"Different second fact"}];cases.write_text(json.dumps(data))
            with self.assertRaises(ValueError):
                with redirect_stdout(io.StringIO()):evaluate_quality.run(args)
            self.assertFalse(args.output.with_suffix('.json').exists())

    def test_default_dataset_and_existing_review_preservation_remain_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            args,_ = self.make_args(directory,custom=False)
            review_path=args.output.with_suffix('.review.json');review_path.write_text('{"preserve":"user review"}')
            with redirect_stdout(io.StringIO()):evaluate_quality.run(args)
            result=json.loads(args.output.with_suffix('.json').read_text())
            self.assertEqual(result['summary']['expected_cases'],12)
            self.assertEqual(review_path.read_text(),'{"preserve":"user review"}')
