"""Pure publication preflight, not semantic accuracy or database-fence tests."""

import json
import unittest

from app.services.graph_publication_candidate import prepare_graph_publication
from app.services.parser_service import DocumentChunk
from tests.test_graph_projection_replacement import graph


class PublicationCandidateTests(unittest.TestCase):
    def setUp(self):
        self.identity = dict(upload_id="upload", course_id="course", document_task_id="task", document_name="sample.pdf")
        self.chunks = [DocumentChunk(id=f"chunk-{i}", text=f"Evidence {i}", metadata={
            "upload_id":"upload", "document_id":"course", "execution_token":"task",
            "document_name":"sample.pdf", "page_number":1}) for i in range(2)]

    def candidate(self, extraction=None, chunks=None):
        return prepare_graph_publication(extraction if extraction is not None else graph(),
            chunks if chunks is not None else self.chunks, **self.identity)

    def test_deterministic_and_frozen_against_later_mutation(self):
        extraction = graph()
        first = self.candidate(extraction)
        self.assertEqual(first, self.candidate(extraction))
        extraction.nodes[0].description = "Changed"
        self.assertNotEqual(first.sha256, self.candidate(extraction).sha256)
        self.assertEqual(json.loads(first.payload_json)["extraction"]["nodes"][0]["description"], "")

    def test_evidence_text_changes_fingerprint(self):
        first = self.candidate()
        chunks = [DocumentChunk(id=self.chunks[0].id, text="Different evidence", metadata=self.chunks[0].metadata), self.chunks[1]]
        self.assertNotEqual(first.sha256, self.candidate(chunks=chunks).sha256)

    def test_foreign_or_duplicate_sources_rejected(self):
        for chunks in ([], [self.chunks[0], self.chunks[0]],
                       [DocumentChunk(id="foreign", text="x", metadata={})]):
            with self.subTest(chunks=chunks), self.assertRaises(ValueError):
                self.candidate(chunks=chunks)

    def test_primary_upload_page_filename_and_chunk_must_match(self):
        for update in ({"upload_id":"other"}, {"page_number":2}, {"document_name":"other.pdf"}, {"source_chunk_id":"foreign"}):
            extraction = graph()
            extraction.nodes[0] = extraction.nodes[0].model_copy(update=update)
            with self.subTest(update=update), self.assertRaises(ValueError):
                self.candidate(extraction)

    def test_plural_provenance_cannot_smuggle_foreign_chunks_or_pages(self):
        for update in ({"source_chunk_ids":["foreign"]}, {"source_chunk_ids":["chunk-0", "foreign"]},
                       {"source_chunk_ids":["chunk-0"], "page_numbers":[2]}):
            extraction = graph()
            extraction.nodes[0] = extraction.nodes[0].model_copy(update=update)
            with self.subTest(update=update), self.assertRaises(ValueError):
                self.candidate(extraction)

    def test_page_numbers_must_be_integers_not_equal_valued_booleans_or_floats(self):
        for update in ({"page_number": True}, {"page_number": 1.0},
                       {"page_numbers": [True]}, {"page_numbers": [1.0]}):
            extraction = graph()
            extraction.nodes[0] = extraction.nodes[0].model_copy(update=update)
            with self.subTest(update=update), self.assertRaises(ValueError):
                self.candidate(extraction)

    def test_section_headings_must_match_referenced_sources(self):
        for update in ({"section_heading": "Fabricated section"},
                       {"section_headings": ["Fabricated section"]}):
            extraction = graph()
            extraction.nodes[0] = extraction.nodes[0].model_copy(update=update)
            with self.subTest(update=update), self.assertRaises(ValueError):
                self.candidate(extraction)

    def test_ingestion_unlabelled_section_fallback_is_accepted(self):
        extraction = graph()
        extraction.nodes[0] = extraction.nodes[0].model_copy(update={
            "section_heading":"Unlabelled section", "section_headings":["Unlabelled section"]})
        self.candidate(extraction)

    def test_post_validation_mutated_relationship_rejected(self):
        for update in ({"relation_type":"INVENTED"}, {"target_node_id":"missing"}):
            extraction = graph()
            extraction.relationships[0] = extraction.relationships[0].model_copy(update=update)
            with self.subTest(update=update), self.assertRaises(ValueError):
                self.candidate(extraction)

    def test_aggregate_not_subject_to_provider_batch_limits(self):
        chunks = [DocumentChunk(id=f"chunk-{i}", text="Evidence", metadata=self.chunks[0].metadata) for i in range(13)]
        self.assertEqual(len(json.loads(self.candidate(graph(13), chunks).payload_json)["extraction"]["nodes"]), 13)
