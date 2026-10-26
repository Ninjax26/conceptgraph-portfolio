"""Offline receipt observation contracts; no Neo4j or PostgreSQL I/O."""

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from app.models.document_upload import GraphPublicationReceipt
from app.services.graph_continuation_service import GraphContinuationService
from app.services.graph_publication_candidate import GraphPublicationCandidate
from app.services.ingestion_service import GraphProjectionMarker


class PublicationReceiptTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.service = GraphContinuationService()
        self.claim = SimpleNamespace(job_id="job", upload_id="upload", document_task_id="task")
        self.record = SimpleNamespace(upload_id="upload", course_uuid="course", task_id="task")
        self.job = SimpleNamespace(id="job")
        self.intent = SimpleNamespace(id="intent", job_id="job", upload_id="upload",
            course_id="course", document_task_id="task", payload_sha256="a" * 64)
        self.marker = GraphProjectionMarker("intent", "job", "upload", "course", "task", "a" * 64, 1)
        self.candidate = GraphPublicationCandidate("{}", "a" * 64)
        self.reader = SimpleNamespace(read_graph_publication_marker=AsyncMock(return_value=self.marker))

    def session(self, *, existing_receipt=None):
        intent_result = Mock()
        intent_result.scalar_one_or_none.return_value = self.intent
        receipt_result = Mock()
        receipt_result.scalar_one_or_none.return_value = existing_receipt
        return SimpleNamespace(execute=AsyncMock(side_effect=[intent_result, receipt_result]),
            flush=AsyncMock(), add=Mock(), commit=AsyncMock(), rollback=AsyncMock())

    async def observe(self, session, *, locks=None):
        locked = (self.record, self.job, object())
        with patch.object(self.service, "load_publication_draft", AsyncMock(return_value=self.candidate)), \
             patch.object(self.service, "_locked_claim", AsyncMock(side_effect=locks or [locked, locked])):
            return await self.service.observe_publication_receipt(session, self.claim, self.reader)

    async def test_receipt_has_no_cascading_source_foreign_key(self):
        self.assertFalse(GraphPublicationReceipt.__table__.foreign_keys)

    async def test_exact_marker_stages_one_caller_owned_receipt(self):
        session = self.session()
        receipt = await self.observe(session)
        self.assertIs(receipt, session.add.call_args.args[0])
        self.assertEqual((receipt.intent_id, receipt.job_id, receipt.upload_id,
            receipt.course_id, receipt.document_task_id, receipt.payload_sha256),
            ("intent", "job", "upload", "course", "task", "a" * 64))
        self.reader.read_graph_publication_marker.assert_awaited_once_with("intent")
        session.commit.assert_not_awaited()

    async def test_absent_or_conflicting_marker_never_stages_receipt(self):
        for marker in (None, GraphProjectionMarker("intent", "other", "upload", "course", "task", "a" * 64, 1)):
            with self.subTest(marker=marker):
                self.reader.read_graph_publication_marker.reset_mock(return_value=True)
                self.reader.read_graph_publication_marker.return_value = marker
                session = self.session()
                if marker is None:
                    self.assertIsNone(await self.observe(session))
                else:
                    with self.assertRaisesRegex(ValueError, "Remote graph marker"):
                        await self.observe(session)
                session.add.assert_not_called()
                session.commit.assert_not_awaited()

    async def test_expired_claim_after_remote_read_does_not_stage(self):
        session = self.session()
        locked = (self.record, self.job, object())
        self.assertIsNone(await self.observe(session, locks=[locked, None]))
        session.add.assert_not_called()

    async def test_exact_receipt_replays_but_conflict_fails_closed(self):
        existing = GraphPublicationReceipt(intent_id="intent", job_id="job", upload_id="upload",
            course_id="course", document_task_id="task", payload_sha256="a" * 64)
        session = self.session(existing_receipt=existing)
        self.assertIs(await self.observe(session), existing)
        session.add.assert_not_called()
        existing.payload_sha256 = "b" * 64
        with self.assertRaisesRegex(ValueError, "receipt conflicts"):
            await self.observe(self.session(existing_receipt=existing))


if __name__ == "__main__":
    unittest.main()
