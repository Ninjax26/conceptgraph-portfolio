"""Unpublished intent boundaries; no remote write or completion is exercised."""

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from app.models.document_upload import GraphPublicationIntent
from app.services.graph_continuation_service import GraphContinuationService
from app.services.graph_publication_candidate import GraphPublicationCandidate


class PublicationIntentTests(unittest.IsolatedAsyncioTestCase):
    def session(self, saved=None):
        result = Mock()
        result.scalar_one_or_none.return_value = saved
        return SimpleNamespace(flush=AsyncMock(), execute=AsyncMock(return_value=result),
            add=Mock(), commit=AsyncMock(), rollback=AsyncMock())

    def claim(self):
        return SimpleNamespace(job_id="job", upload_id="upload", document_task_id="task")

    def locked(self):
        record = SimpleNamespace(upload_id="upload", task_id="task", course_uuid="course",
            attempt_count=2)
        return record, SimpleNamespace(id="job"), object()

    async def test_intent_has_no_cascading_document_or_job_foreign_key(self):
        self.assertFalse(GraphPublicationIntent.__table__.foreign_keys)

    async def test_deleted_source_discovery_is_bounded_and_read_only(self):
        service = GraphContinuationService()
        rows = [SimpleNamespace(id="old-a"), SimpleNamespace(id="old-b")]
        result = Mock()
        result.scalars.return_value.all.return_value = rows
        session = SimpleNamespace(execute=AsyncMock(return_value=result),
            commit=AsyncMock(), rollback=AsyncMock(), add=Mock())
        self.assertEqual(await service.list_deleted_source_intents(session, limit=2), rows)
        query = str(session.execute.call_args.args[0])
        self.assertIn("graph_publication_intents", query)
        self.assertIn("document_uploads", query)
        self.assertIn("IS NULL", query)
        self.assertIn("LIMIT", query)
        session.add.assert_not_called()
        session.commit.assert_not_awaited()
        session.rollback.assert_not_awaited()
        for bad in (0, 101, True, 1.5):
            with self.assertRaises(ValueError):
                await service.list_deleted_source_intents(session, limit=bad)

    async def test_lost_authority_or_missing_draft_creates_no_intent(self):
        service = GraphContinuationService()
        for saved, locked in ((None, self.locked()),
                              (GraphPublicationCandidate("{}", "a" * 64), None)):
            session = self.session()
            with patch.object(service, "load_publication_draft", AsyncMock(return_value=saved)), \
                 patch.object(service, "_locked_claim", AsyncMock(return_value=locked)):
                self.assertIsNone(await service.stage_publication_intent(session, self.claim()))
            session.add.assert_not_called()
            session.commit.assert_not_awaited()

    async def test_new_intent_is_caller_owned_and_bound_to_exact_candidate(self):
        service, session = GraphContinuationService(), self.session()
        candidate = GraphPublicationCandidate("{}", "a" * 64)
        with patch.object(service, "load_publication_draft", AsyncMock(return_value=candidate)), \
             patch.object(service, "_locked_claim", AsyncMock(return_value=self.locked())):
            intent = await service.stage_publication_intent(session, self.claim())
        self.assertIs(intent, session.add.call_args.args[0])
        self.assertEqual((intent.job_id, intent.upload_id, intent.document_task_id,
            intent.course_id, intent.payload_sha256, intent.source_generation),
            ("job", "upload", "task", "course", "a" * 64, 2))
        session.commit.assert_not_awaited()
        session.rollback.assert_not_awaited()

    async def test_same_intent_is_idempotent_but_conflicting_identity_is_rejected(self):
        service = GraphContinuationService()
        candidate = GraphPublicationCandidate("{}", "a" * 64)
        existing = SimpleNamespace(job_id="job", upload_id="upload", document_task_id="task",
            course_id="course", payload_sha256="a" * 64, source_generation=2)
        session = self.session(existing)
        with patch.object(service, "load_publication_draft", AsyncMock(return_value=candidate)), \
             patch.object(service, "_locked_claim", AsyncMock(return_value=self.locked())):
            self.assertIs(await service.stage_publication_intent(session, self.claim()), existing)
        session.add.assert_not_called()
        existing.payload_sha256 = "b" * 64
        with patch.object(service, "load_publication_draft", AsyncMock(return_value=candidate)), \
             patch.object(service, "_locked_claim", AsyncMock(return_value=self.locked())):
            with self.assertRaisesRegex(ValueError, "different"):
                await service.stage_publication_intent(session, self.claim())
        session.add.assert_not_called()
        session.commit.assert_not_awaited()

    async def test_old_intent_without_generation_fails_closed(self):
        service = GraphContinuationService()
        existing = SimpleNamespace(job_id="job", upload_id="upload", document_task_id="task",
            course_id="course", payload_sha256="a" * 64, source_generation=None)
        session = self.session(existing)
        with patch.object(service, "load_publication_draft", AsyncMock(
                return_value=GraphPublicationCandidate("{}", "a" * 64))), \
             patch.object(service, "_locked_claim", AsyncMock(return_value=self.locked())):
            with self.assertRaisesRegex(ValueError, "different"):
                await service.stage_publication_intent(session, self.claim())
        session.add.assert_not_called()


if __name__ == "__main__":
    unittest.main()
