"""Explicit facts must belong to the requested device and survive citation trimming."""
import copy
import json
import unittest
from unittest.mock import patch

from app.services.citation_service import prepare_evidence
from app.services.evidence_trace import EvidenceTrace


def chunk(text, page=1, rerank=6.):
    return {'id':str(page),'text':text,'score':.9,'rerank_score':rerank,
            'metadata':{'upload_id':'authored','document_name':'fixture.pdf','page_number':page}}


class FactSupportTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch('app.core.config.settings.evidence_min_score',.35))
        self.enterContext(patch('app.core.config.settings.retrieval_verify_identifiers',True))

    def test_missing_manufacturer_and_voltage_refuse_despite_high_relevance(self):
        items=[chunk('Equipment PMP-104. The approved pressure is 11 bar.')]
        for question in ('Who manufactured PMP-104?','What is the supply voltage of PMP-104?'):
            with self.subTest(question=question):
                sources,confidence=prepare_evidence(question,items)
                self.assertEqual(sources,[])
                self.assertEqual(confidence['level'],'insufficient')

    def test_explicit_properties_allow_supported_values(self):
        cases=[
            ('Who manufactured PMP-104?','Equipment identifier PMP-104. Manufacturer: Acme Systems.'),
            ('Who makes PMP-104?','PMP-104 is manufactured by Acme Systems.'),
            ('What is the input voltage of PMP-104?','Equipment PMP-104. Input voltage: 230 V AC.'),
            ('Give the supply voltage of PMP-104.','Equipment PMP-104. Supply voltage: 110-120 V AC.'),
            ('What is the output voltage of PMP-104?','PMP-104 output voltage: 5 V DC.'),
            ('What serial number is listed for PMP-104?','Equipment identifier PMP-104. Serial number: AX-901.'),
        ]
        for question,text in cases:
            with self.subTest(question=question,text=text):
                sources,_=prepare_evidence(question,[chunk(text)])
                self.assertEqual(len(sources),1)
                self.assertEqual(sources[0]['supporting_passage'],text)

    def test_unknown_negated_conditional_and_generic_statements_are_not_facts(self):
        texts=['PMP-104 manufacturer is unknown.','PMP-104 manufacturer is not specified.',
               'PMP-104 is not manufactured by Acme.','PMP-104 might be manufactured by Acme.',
               'Example: PMP-104 is manufactured by Acme.','PMP-104 has a manufacturer.',
               'The manual for PMP-104 is made by Acme.','PMP-104 Brand: Acme.']
        for text in texts:
            with self.subTest(text=text):
                self.assertEqual(prepare_evidence('Who manufactured PMP-104?',[chunk(text)])[0],[])

    def test_other_device_and_output_voltage_do_not_answer_input_voltage(self):
        texts=['PMP-104 pressure is 11 bar. PMP-105 input voltage: 24 V DC.',
               'Equipment PMP-104. Input voltage not recorded. Equipment PMP-105. Input voltage: 24 V DC.',
               'Equipment PMP-104. Output voltage: 24 V DC.',
               'PMP-104 input voltage is not specified.']
        for text in texts:
            with self.subTest(text=text):
                self.assertEqual(prepare_evidence('What is the input voltage of PMP-104?',[chunk(text)])[0],[])

    def test_requested_property_must_be_in_literal_cited_window(self):
        item=chunk('PMP-104 pressure is 11 bar. Manufacturer: Acme Systems.')
        item['rerank_passage']='PMP-104 pressure is 11 bar.'
        self.assertEqual(prepare_evidence('Who manufactured PMP-104?',[item])[0],[])

    def test_fact_cannot_rescue_below_threshold_or_wrong_identifier(self):
        self.assertEqual(prepare_evidence('Who manufactured PMP-104?',
            [dict(chunk('PMP-104 manufacturer: Acme.',rerank=-10.),score=.1)])[0],[])
        self.assertEqual(prepare_evidence('Who manufactured PMP-104?',
            [chunk('PMP-105 manufacturer: Acme.')])[0],[])

    def test_definition_and_other_supported_attributes_keep_existing_path(self):
        for question,text in [('What does voltage mean for PMP-104?', 'PMP-104 notes: voltage is electric potential difference.'),
                              ('What pressure limit applies to PMP-104?','PMP-104 pressure limit is 11 bar.')]:
            self.assertEqual(len(prepare_evidence(question,[chunk(text)])[0]),1)

    def test_comparison_requires_each_devices_own_fact(self):
        question='Compare the manufacturer of PMP-104 with the manufacturer of PMP-105.'
        items=[dict(chunk('PMP-104 manufacturer: Acme.'),evidence_facet=0,evidence_facet_count=2),
               dict(chunk('PMP-105 pressure: 12 bar.',2),evidence_facet=1,evidence_facet_count=2)]
        self.assertEqual(prepare_evidence(question,items)[0],[])
        items[1]['text']='PMP-105 manufacturer: Beacon.'
        sources,_=prepare_evidence(question,items)
        self.assertEqual({s['page_number'] for s in sources},{1,2})

    def test_citation_budget_reserves_distinct_requested_properties(self):
        items=[chunk(f'PMP-104 manufacturer: Acme. Reference {i}.',i) for i in range(1,7)]
        items.append(chunk('PMP-104 input voltage: 230 V.',7,rerank=4.))
        sources,_=prepare_evidence('What are the manufacturer and input voltage of PMP-104?',items)
        self.assertLessEqual(len(sources),5)
        self.assertIn(7,{s['page_number'] for s in sources})
        self.assertTrue(any('manufacturer: Acme' in s['supporting_passage'] for s in sources))

    def test_fact_guard_trace_is_source_safe_and_input_is_unchanged(self):
        items=[chunk('PMP-104 pressure is 11 bar.')];before=copy.deepcopy(items);trace=EvidenceTrace()
        prepare_evidence('Who manufactured PMP-104?',items,trace=trace)
        self.assertEqual(items,before)
        self.assertTrue(any(e['stage']=='fact_coverage' and e['reason']=='missing_requested_fact' for e in trace.events))
        self.assertNotIn('PMP-104',json.dumps(trace.events))
        self.assertNotIn('11 bar',json.dumps(trace.events))

    def test_request_binding_does_not_cross_compared_properties(self):
        from app.services.fact_support import requested_facts
        facts=requested_facts('Compare the manufacturer of PMP-104 with the input voltage of PMP-105.')
        self.assertEqual([(f.attribute,f.identifier,f.qualifier) for f in facts],
                         [('manufacturer','pmp104',None),('voltage','pmp105','input')])

    def test_comparison_fact_must_pass_its_own_facet(self):
        question='Compare the manufacturer of PMP-104 with the input voltage of PMP-105.'
        items=[dict(chunk('PMP-104 pressure is 11 bar.'),evidence_facet=0,evidence_facet_count=2),
               dict(chunk('PMP-104 manufacturer: Acme. PMP-105 input voltage: 24 V.',2),evidence_facet=1,evidence_facet_count=2)]
        self.assertEqual(prepare_evidence(question,items)[0],[])

    def test_serial_description_and_tentative_manufacturer_are_not_values(self):
        for question,text in [
            ('What serial number is listed for PMP-104?','PMP-104 serial number is required.'),
            ('What serial number is listed for PMP-104?','PMP-104 serial numbers are recorded elsewhere.'),
            ('Who manufactured PMP-104?','PMP-104 would be manufactured by Acme.'),
            ('Who manufactured PMP-104?','If ordered, PMP-104 is manufactured by Acme.'),
        ]:
            with self.subTest(text=text):
                self.assertEqual(prepare_evidence(question,[chunk(text)])[0],[])
        self.assertEqual(len(prepare_evidence('What serial number is listed for PMP-104?',
            [chunk('Equipment PMP-104. Serial number: ZXCV.')])[0]),1)

    def test_mixed_attribute_clauses_keep_their_own_subjects_and_context(self):
        from app.services.fact_support import requested_facts
        question='What manufacturer made PMP-104 and what pressure limit applies to PMP-105?'
        self.assertEqual([(f.attribute,f.identifier) for f in requested_facts(question)],[('manufacturer','pmp104')])
        sources,_=prepare_evidence(question,[chunk('PMP-104 manufacturer: Acme.'),chunk('PMP-105 pressure limit: 12 bar.',2)])
        self.assertEqual({s['page_number'] for s in sources},{1,2})

    def test_manufacturer_suffix_cannot_reassign_another_devices_fact(self):
        text='PMP-104 has a pressure limit. Manufacturer: Acme for PMP-105.'
        self.assertEqual(prepare_evidence('Who manufactured PMP-104?',[chunk(text)])[0],[])

    def test_example_context_and_conditional_active_statements_are_not_facts(self):
        for text in ['Hypothetical example. Equipment PMP-104. Manufacturer: Acme.',
                     'Acme manufactures PMP-104 if ordered.']:
            with self.subTest(text=text):
                self.assertEqual(prepare_evidence('Who manufactures PMP-104?',[chunk(text)])[0],[])
        self.assertEqual(len(prepare_evidence('Who manufactures PMP-104?',
            [chunk('Acme Systems manufactures PMP-104.')])[0]),1)

    def test_many_explicit_facts_fail_closed_before_expanding_coverage_states(self):
        question='List manufacturer voltage serial number for PMP-101 PMP-102 PMP-103.'
        sources,confidence=prepare_evidence(question,[chunk(question+' Manufacturer: Acme. Voltage: 24 V. Serial number: AX-901.')])
        self.assertEqual(sources,[])
        self.assertIn('too many explicit facts',confidence['reason'])

    def test_fact_loss_diagnostics_preserve_earlier_search_and_score_failures(self):
        from scripts.evaluate_retrieval_scaling import diagnose_source_losses
        expected=[{'document_name':'fixture.pdf','page_number':1}]
        for items,stage in [([], 'search'),([dict(chunk('PMP-104 manufacturer: Acme.',rerank=-10.),score=.1)],'evidence_gate'),
                            ([chunk('PMP-104 pressure limit: 11 bar.')],'fact_coverage')]:
            with self.subTest(stage=stage):
                trace=EvidenceTrace()
                sources,_=prepare_evidence('Who manufactured PMP-104?',items,trace=trace)
                losses=diagnose_source_losses(expected,items,items,[],sources,trace_events=trace.events)
                self.assertEqual(losses[0]['stage'],stage)

    def test_generic_company_descriptions_and_questions_are_not_named_facts(self):
        texts=['The company that manufactures PMP-104 is not identified.',
               'PMP-104 manufacturer: a qualified vendor.',
               'Acme Systems manufactures PMP-104?']
        for text in texts:
            with self.subTest(text=text):
                self.assertEqual(prepare_evidence('Who manufactured PMP-104?',[chunk(text)])[0],[])

    def test_review_suffix_polarity_does_not_establish_a_fact(self):
        for question,text in [
            ('What is the input voltage of PMP-104?','PMP-104 input voltage: 24 V DC is not the correct value.'),
            ('What is the input voltage of PMP-104?','PMP-104 input voltage: 24 V DC is unconfirmed.'),
            ('What serial number is listed for PMP-104?','PMP-104 serial number: AX-901 is not confirmed.'),
            ('Who manufactures PMP-104?','Acme manufactures PMP-104, but this is unconfirmed.'),
        ]:
            with self.subTest(text=text):
                self.assertEqual(prepare_evidence(question,[chunk(text)])[0],[])
        for text in ['PMP-104 input voltage: 24 V DC. Serial number is not recorded.',
                     'PMP-104 input voltage: 24 V DC; serial number is not recorded.']:
            self.assertEqual(len(prepare_evidence('What is the input voltage of PMP-104?',[chunk(text)])[0]),1)

    def test_review_forward_or_accessory_binding_cannot_borrow_fact(self):
        for question,text in [
            ('What is the input voltage of PMP-104?','Input voltage: 24 V DC. PMP-104 input voltage is not recorded.'),
            ('What is the input voltage of PMP-104?','PMP-104 is discussed here. The other device has input voltage: 24 V DC.'),
            ('What is the input voltage of PMP-104?','The adapter for PMP-104 has input voltage: 24 V DC.'),
            ('Who manufactures PMP-104?','The label attached to PMP-104 is manufactured by Acme Labels.'),
        ]:
            with self.subTest(text=text):
                self.assertEqual(prepare_evidence(question,[chunk(text)])[0],[])
        self.assertEqual(len(prepare_evidence('What is the input voltage of PMP-104?',
            [chunk('Adapter PMP-104 has input voltage: 24 V DC.')])[0]),1)

    def test_review_placeholders_and_references_are_not_concrete_values(self):
        for text in ['PMP-104 manufacturer: see nameplate.','PMP-104 manufacturer: to be determined.',
                     'Equipment PMP-104. Serial number: NA.','Equipment PMP-104. Serial number: TBA.',
                     'Equipment PMP-104. Serial number: PENDING.']:
            question='What serial number is listed for PMP-104?' if 'Serial' in text else 'Who manufactured PMP-104?'
            with self.subTest(text=text):
                self.assertEqual(prepare_evidence(question,[chunk(text)])[0],[])

    def test_table_rows_do_not_borrow_a_value_from_another_device(self):
        text='Equipment PMP-104. Input voltage not recorded. Equipment PMP-105. Input voltage: 24 V DC.'
        self.assertEqual(prepare_evidence('What input voltage applies to PMP-104?',[chunk(text)])[0],[])
        self.assertEqual(len(prepare_evidence('What input voltage applies to PMP-105?',[chunk(text)])[0]),1)


if __name__=='__main__':
    unittest.main()
