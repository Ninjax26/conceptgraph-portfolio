"""Supporting matches extend established evidence; they never unlock refusals."""
import asyncio
import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from pydantic import SecretStr

from app.services.citation_service import prepare_evidence
from app.services.rerank_service import RerankService


def candidate(index, text, score=5., supporting=False):
    return dict(id=str(index), text=text, score=.8, rerank_score=score,
                supporting_evidence=supporting,
                metadata=dict(upload_id='fixture', page_number=index, document_name='authored.pdf'))


class SupportingEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch('app.core.config.settings.evidence_min_score', .35))
        self.enterContext(patch('app.core.config.settings.retrieval_verify_identifiers', True))

    def test_supporting_plan_retains_only_unambiguous_explicit_intents(self):
        from app.services.evidence_facets import supporting_query
        cases = {
            'What must I understand directly before HTTPS?': 'direct prerequisite for HTTPS',
            'What should I learn before idempotency?': 'prerequisites for idempotency',
            'Why does a signed login session still need authorization checks for each document?': 'authorization checks for each document',
            'Why must vector retrieval use the same document scope as access control?': 'vector retrieval',
            'Why cannot a PostgreSQL rollback remove partial Qdrant writes?': 'Can a PostgreSQL rollback undo writes outside the transaction?',
            'Why can’t a database rollback undo index writes?': 'Can a database rollback undo writes outside the transaction?',
            'Why cannot a PostgreSQL rollback remove Qdrant writes after retry?': None,
            'Why cannot a PostgreSQL rollback remove Qdrant writes and object uploads?': None,
            'Why cannot a PostgreSQL rollback remove a blob?': None,
            'Why cannot a firmware rollback undo disk writes?': None,
            'Who manufactured PMP-104?': None,
            'Compare TLS with authorization.': None,
            'What must I learn before HTTPS and SQL?': None,
            'Why does TLS still need authorization, and how?': None,
            'What should I not learn before HTTPS?': None,
            'Why must A not use the same scope as B?': None,
            'Why must A use the same scope as B: explain?': None,
            'Why must vector retrieval use the same document scope as access control only for public uploads?': None,
            'Why must vector retrieval use the same document scope as access control when no user is signed in?': None,
            'Why must vector retrieval use the same document scope as access control rather than ingestion?': None,
            'Why does a signed login session still need authorization checks if all files are public?': None,
            'What should I learn before TLS unless I know HTTP?': None,
            'Why must vector retrieval use the same document scope as access control for public uploads?': None,
            'Why does a login session in anonymous mode still need document permissions?': None,
        }
        for question, expected in cases.items():
            with self.subTest(question=question):
                self.assertEqual(supporting_query(question), expected)

    def test_extra_high_scores_cannot_unlock_a_refused_primary(self):
        primary = candidate(1, 'Unrelated source material.', -10.)
        extra = candidate(2, 'Topical prerequisite material.', supporting=True)
        expected = prepare_evidence('What must I understand directly before HTTPS?', [primary])
        self.assertEqual(prepare_evidence('What must I understand directly before HTTPS?', [primary, extra]), expected)
        self.assertEqual(expected[0], [])

    def test_supported_primary_retains_citations_and_adds_new_page(self):
        question = 'Why must vector retrieval use the same document scope as access control?'
        primary = candidate(1, 'Access control checks the document scope.')
        extra = candidate(2, 'Vector search filters allowed upload identifiers.', supporting=True)
        original = prepare_evidence(question, [primary])
        before = copy.deepcopy([primary, extra])
        sources, confidence = prepare_evidence(question, [primary, extra])
        self.assertEqual(len(sources), 2)
        self.assertEqual(sources[0], original[0][0])
        self.assertEqual(sources[1]['page_number'], 2)
        self.assertEqual(confidence['score'], original[1]['score'])
        self.assertEqual(confidence['level'], original[1]['level'])
        self.assertEqual([primary, extra], before)

    def test_rollback_context_uses_scored_evidence_and_keeps_the_direct_answer_first(self):
        from app.services.synthesis_service import SynthesisService

        question = "Why cannot a PostgreSQL rollback remove partial Qdrant writes?"
        cleanup = "A PostgreSQL rollback cannot erase points already written to Qdrant."
        scope = "A SQL transaction does not automatically roll back writes made to an external vector database."
        service = RerankService()
        service._model = SimpleNamespace(predict=lambda pairs: [
            (5. if "Qdrant" in passage else -8.) if prompt == question else
            (4. if "external vector database" in passage else -8.)
            for prompt, passage in pairs
        ])
        ranked = service._rerank_locally(question, [candidate(1, scope), candidate(4, cleanup)])
        sources, confidence = prepare_evidence(question, ranked)
        primary_sources, primary_confidence = prepare_evidence(
            question, [chunk for chunk in ranked if not chunk.get("supporting_evidence")]
        )
        self.assertEqual(sources[:1], primary_sources)
        self.assertEqual(confidence["score"], primary_confidence["score"])
        self.assertEqual([s["page_number"] for s in sources], [4, 1])
        prompt = SynthesisService._user_prompt(question, [], sources)
        self.assertIn(question, prompt)
        self.assertIn(cleanup, prompt)
        self.assertIn(scope, prompt)

    def test_saturated_primary_citations_and_identifiers_cannot_be_displaced(self):
        primary = [candidate(i, f'PMP-104 reference {i}.') for i in range(1,6)]
        extra = candidate(9, 'PMP-105 pressure information.', score=10., supporting=True)
        self.assertEqual(prepare_evidence('PMP-104 pressure?', [*primary, extra]),
                         prepare_evidence('PMP-104 pressure?', primary))
        primary = primary[:1]
        self.assertEqual(prepare_evidence('PMP-104 pressure?', [*primary, extra])[0],
                         prepare_evidence('PMP-104 pressure?', primary)[0])

    def test_optional_rerank_failure_preserves_original_result(self):
        service = RerankService()
        calls = []
        def predict(pairs):
            calls.append(pairs)
            if len(calls) == 2:
                raise RuntimeError('optional model unavailable')
            return [5.] * len(pairs)
        service._model = SimpleNamespace(predict=predict)
        chunks = [candidate(1, 'TLS is a direct prerequisite for HTTPS.')]
        ranked = service._rerank_locally('What must I understand directly before HTTPS?', chunks)
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(ranked), 1)
        self.assertFalse(ranked[0].get('supporting_evidence'))

    def test_refused_primary_does_not_spend_a_supporting_call(self):
        service = RerankService()
        calls = []
        def predict(pairs):
            calls.append(pairs)
            return [-10.] * len(pairs)
        service._model = SimpleNamespace(predict=predict)
        ranked = service._rerank_locally('What must I understand directly before HTTPS?', [candidate(1,'Unrelated')])
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(ranked), 1)

    def test_full_citation_budget_does_not_spend_optional_model_work(self):
        service = RerankService()
        calls = []
        def predict(pairs):
            calls.append(pairs)
            return [5.] * len(pairs)
        service._model = SimpleNamespace(predict=predict)
        chunks = [candidate(i, f'TLS prerequisite reference {i}.') for i in range(1,6)]
        ranked = service._rerank_locally('What must I understand directly before HTTPS?', chunks)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(ranked), 5)

    def test_optional_sources_prefer_new_pages_and_explain_cap(self):
        from app.services.evidence_trace import EvidenceTrace
        trace = EvidenceTrace()
        primary = candidate(1, 'Primary literal passage.')
        extra = [dict(candidate(1, f'Distinct supporting passage {i}.', supporting=True), id=f'extra-{i}') for i in range(6)]
        extra.append(candidate(2, 'An additional useful page.', score=4., supporting=True))
        sources, _ = prepare_evidence('Explain these sources', [primary,*extra],trace=trace)
        self.assertEqual(len(sources), 5)
        self.assertEqual(sources[0]['supporting_passage'], 'Primary literal passage.')
        self.assertEqual(sources[1]['page_number'],2)
        self.assertEqual(sum(e['stage']=='supporting_selection' and e['reason']=='citation_limit' for e in trace.events),3)
        self.assertEqual(len([e for e in trace.events if e['stage']=='final_source']),5)

    def test_optional_context_without_identifier_keeps_primary_identifier_guard(self):
        primary=candidate(1,'PMP-104 has a pressure specification.')
        extra=candidate(2,'Supporting background for interpreting pressure.',supporting=True)
        wrong=candidate(3,'PMP-105 has different pressure.',supporting=True)
        sources,_=prepare_evidence('PMP-104 pressure?', [primary,extra,wrong])
        self.assertEqual([s['page_number'] for s in sources],[1,2])
        self.assertIn('PMP-104',sources[0]['supporting_passage'])

    def test_hosted_optional_quota_error_preserves_primary(self):
        async def run():
            calls=[]
            def response(request):
                calls.append(json.loads(request.content))
                if len(calls)==2:
                    return httpx.Response(429,json={'message':'quota'})
                return httpx.Response(200,json={'results':[{'index':0,'relevance_score':.9}]})
            async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
                with patch('app.core.config.settings.cohere_api_key',SecretStr('offline-test')):
                    ranked=await RerankService(client)._rerank_with_cohere('What must I understand directly before HTTPS?',
                        [candidate(1,'TLS is a prerequisite for HTTPS.')])
            self.assertEqual(len(calls),2)
            self.assertEqual(len(ranked),1)
            self.assertFalse(ranked[0].get('supporting_evidence'))
        asyncio.run(run())

    def test_hosted_support_is_bounded_and_cancellation_propagates(self):
        async def run(cancel=False):
            requests = []
            async def response(request):
                payload=json.loads(request.content); requests.append(payload)
                if len(requests)==2 and cancel:
                    raise asyncio.CancelledError()
                return httpx.Response(200,json={'results':[{'index':i,'relevance_score':.9} for i in range(len(payload['documents']))]})
            async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
                with patch('app.core.config.settings.cohere_api_key',SecretStr('offline-test')):
                    service=RerankService(client)
                    chunks=[candidate(1,'TLS is a prerequisite for HTTPS.')]
                    if cancel:
                        with self.assertRaises(asyncio.CancelledError):
                            await service._rerank_with_cohere('What must I understand directly before HTTPS?',chunks)
                    else:
                        ranked=await service._rerank_with_cohere('What must I understand directly before HTTPS?',chunks)
                        self.assertEqual(len(requests),2)
                        self.assertTrue(any(item.get('supporting_evidence') for item in ranked))
                        self.assertEqual(requests[1]['query'],'direct prerequisite for HTTPS')
                        self.assertTrue(all(passage in chunks[0]['text'] for request in requests for passage in request['documents']))
                        self.assertEqual(len(prepare_evidence('What must I understand directly before HTTPS?',ranked)[0]),1)
        asyncio.run(run())
        asyncio.run(run(True))
