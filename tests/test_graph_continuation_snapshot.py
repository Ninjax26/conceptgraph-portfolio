"""No database/provider calls: immutable graph-resume evidence boundaries."""

import asyncio
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from app.services.graph_continuation_service import create_snapshot, extraction_contract, snapshot_chunks, snapshot_digest
from app.services.graph_continuation_service import GraphContinuationService
from app.services.parser_service import DocumentChunk


class GraphContinuationSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.record = SimpleNamespace(upload_id="upload-a", task_id="task-a", course_uuid="course-a",
            content_hash="a" * 64, storage_key="fixture/source.pdf", original_filename="fixture.pdf", page_count=3,
            status="ready", stage="READY", graph_status="GRAPH_PARTIAL", attempt_count=1,
            processed_chunk_count=1, graph_node_count=1, result_json={"graph_provider_limited": True},
            created_at=datetime.now(timezone.utc))
        self.chunk = DocumentChunk(id="chunk-a", text="DNS maps domain names to IP addresses.", metadata={
            "chunk_id": "chunk-a", "document_id": "course-a", "upload_id": "upload-a", "execution_token": "task-a",
            "document_name": "fixture.pdf", "page_number": 2, "section_heading": "DNS"})

    def test_serialized_snapshot_round_trips_without_reparsing(self):
        snapshot = create_snapshot(self.record, [self.chunk])
        decoded = json.loads(json.dumps(snapshot))
        chunks = snapshot_chunks(decoded, self.record)
        self.assertEqual(chunks, (self.chunk,))
        self.assertEqual(snapshot_digest(snapshot), snapshot_digest(decoded))

    def test_source_task_and_course_identity_cannot_be_rebound(self):
        snapshot = create_snapshot(self.record, [self.chunk])
        for key in ("upload_id", "task_id", "course_uuid", "content_hash", "storage_key", "document_name"):
            with self.assertRaises(ValueError):
                snapshot_chunks({**snapshot, key: "different"}, self.record)
        with self.assertRaises(ValueError):
            snapshot_chunks({**snapshot, "version": True}, self.record)
        with self.assertRaises(ValueError):
            snapshot_chunks({**snapshot, "extra": "unsupported"}, self.record)

    def test_foreign_chunk_metadata_is_rejected(self):
        for key in ("upload_id", "document_id", "execution_token", "document_name", "chunk_id"):
            changed = DocumentChunk(id=self.chunk.id, text=self.chunk.text, metadata={**self.chunk.metadata, key: "different"})
            with self.assertRaises(ValueError):
                create_snapshot(self.record, [changed])

    def test_chunks_are_nonempty_unique_and_bounded(self):
        with self.assertRaises(ValueError):
            create_snapshot(self.record, [])
        with self.assertRaises(ValueError):
            create_snapshot(self.record, [self.chunk, self.chunk])
        with self.assertRaises(ValueError):
            create_snapshot(self.record, [DocumentChunk(id=self.chunk.id, text=" ", metadata=self.chunk.metadata)])
        with patch("app.services.graph_continuation_service.MAX_SNAPSHOT_BYTES", 10):
            with self.assertRaises(ValueError):
                create_snapshot(self.record, [self.chunk])

    def test_pages_are_valid_pdf_pages_not_boolean_or_out_of_range(self):
        for page in (0, -1, True, 4, "2"):
            changed = DocumentChunk(id=self.chunk.id, text=self.chunk.text, metadata={**self.chunk.metadata, "page_number": page})
            with self.assertRaises(ValueError):
                create_snapshot(self.record, [changed])

    def test_nonfinite_metadata_is_not_persisted(self):
        changed = DocumentChunk(id=self.chunk.id, text=self.chunk.text,
            metadata={**self.chunk.metadata, "ocr_confidence": float("nan")})
        with self.assertRaises(ValueError):
            create_snapshot(self.record, [changed])

    def test_returned_chunks_do_not_mutate_the_snapshot(self):
        snapshot = create_snapshot(self.record, [self.chunk])
        chunks = snapshot_chunks(snapshot, self.record)
        chunks[0].metadata["page_number"] = 99
        self.assertEqual(snapshot["chunks"][0]["metadata"]["page_number"], 2)
        self.assertEqual(self.chunk.metadata["page_number"], 2)

    def test_contract_invalidation_uses_model_and_global_linking(self):
        initial = extraction_contract([self.chunk])
        with patch("app.services.ingestion_service.settings.llm_provider", "gemini"), \
             patch("app.services.ingestion_service.settings.gemini_model", "fixture-new-model"):
            self.assertNotEqual(initial, extraction_contract([self.chunk]))
        from app.core.config import settings
        with patch("app.services.graph_continuation_service.settings.graph_global_linking_enabled", not settings.graph_global_linking_enabled):
            self.assertNotEqual(initial, extraction_contract([self.chunk]))

    def test_claim_rechecks_fresh_pending_deadline_before_spending_attempt(self):
        now = datetime.now(timezone.utc)
        job = SimpleNamespace(status="pending", next_attempt_at=now + timedelta(seconds=120), attempt_count=0)
        session = SimpleNamespace(execute=AsyncMock(side_effect=[
            Mock(scalar_one_or_none=Mock(return_value="upload-a")),
            Mock(scalar_one=Mock(return_value=now)),
        ]), rollback=AsyncMock(), commit=AsyncMock())
        service = GraphContinuationService()
        with patch.object(service, "_document", AsyncMock(return_value=self.record)), \
             patch.object(service, "_job", AsyncMock(return_value=job)):
            result = asyncio.run(service.claim_next(session, lease_owner="worker-a", lease_seconds=180))
        self.assertIsNone(result)
        self.assertEqual(job.attempt_count, 0)
        session.rollback.assert_awaited_once()
        session.commit.assert_not_awaited()
