"""Internal draft boundaries, not remote-publication or crash-recovery tests."""

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from app.services.graph_continuation_service import GraphContinuationService
from app.services.graph_publication_candidate import GraphPublicationCandidate


class PublicationDraftTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_capacity_does_not_query(self):
        session = self.session()
        with patch("app.services.graph_continuation_service.settings", SimpleNamespace(graph_draft_max_jobs=0, graph_draft_max_bytes=0)):
            self.assertTrue(await GraphContinuationService._draft_capacity(session, GraphPublicationCandidate("{}", "a"*64)))
        session.execute.assert_not_awaited()

    async def test_capacity_uses_utf8_bytes_and_count_boundaries(self):
        for count, size, expected in ((2, 84, True), (1, 84, False), (2, 83, False)):
            result = Mock()
            result.one.return_value = SimpleNamespace(jobs=1, bytes=80)
            session = self.session()
            session.execute.side_effect = [None, result]
            with self.subTest(count=count, size=size), patch("app.services.graph_continuation_service.settings",
                    SimpleNamespace(graph_draft_max_jobs=count, graph_draft_max_bytes=size)):
                self.assertEqual(await GraphContinuationService._draft_capacity(session, GraphPublicationCandidate("🙂", "a"*64)), expected)

    async def test_invalid_retention_never_queries(self):
        session = self.session()
        for days, limit in ((0, 1), (True, 1), (366, 1), (1, 0), (1, True), (1, 101)):
            with self.subTest(days=days, limit=limit), self.assertRaises(ValueError):
                await GraphContinuationService().prune_terminal_drafts(session, older_than_days=days, limit=limit)
        session.execute.assert_not_awaited()

    async def test_budget_denial_does_not_stage_or_commit(self):
        session, service = self.session(), GraphContinuationService()
        with patch.object(service, "restore_publication", AsyncMock(return_value="validated")), \
             patch.object(service, "_draft_capacity", AsyncMock(return_value=False)):
            self.assertFalse(await service.save_publication_draft(session, SimpleNamespace(job_id="job"), GraphPublicationCandidate("{}", "a"*64)))
        session.add.assert_not_called()
        session.commit.assert_not_awaited()

    def session(self, saved=None):
        result = Mock()
        result.scalar_one_or_none.return_value = saved
        return SimpleNamespace(flush=AsyncMock(), execute=AsyncMock(return_value=result),
            add=Mock(), commit=AsyncMock(), rollback=AsyncMock())

    async def test_lost_authority_saves_nothing(self):
        service, session = GraphContinuationService(), self.session()
        with patch.object(service, "restore_publication", AsyncMock(return_value=None)):
            self.assertFalse(await service.save_publication_draft(session, SimpleNamespace(), "candidate"))
        session.execute.assert_not_awaited()
        session.add.assert_not_called()

    async def test_insert_is_caller_owned_and_same_candidate_is_idempotent(self):
        service, candidate = GraphContinuationService(), GraphPublicationCandidate("{}", "a"*64)
        for saved in (None, SimpleNamespace(payload_json="{}", payload_sha256="a"*64)):
            session = self.session(saved)
            with patch.object(service, "restore_publication", AsyncMock(return_value="validated")):
                self.assertTrue(await service.save_publication_draft(session, SimpleNamespace(job_id="job"), candidate))
            self.assertEqual(session.add.call_count, int(saved is None))
            session.flush.assert_awaited_once()
            session.commit.assert_not_awaited()
            session.rollback.assert_not_awaited()

    async def test_existing_different_candidate_cannot_be_replaced(self):
        service = GraphContinuationService()
        session = self.session(SimpleNamespace(payload_json="old", payload_sha256="a"*64))
        with patch.object(service, "restore_publication", AsyncMock(return_value="validated")):
            with self.assertRaisesRegex(ValueError, "different"):
                await service.save_publication_draft(session, SimpleNamespace(job_id="job"), GraphPublicationCandidate("new", "b"*64))
        session.add.assert_not_called()
        session.commit.assert_not_awaited()

    async def test_oversized_candidate_is_not_staged(self):
        service, session = GraphContinuationService(), self.session()
        with patch.object(service, "restore_publication", AsyncMock(return_value="validated")), \
             patch("app.services.graph_continuation_service.MAX_SNAPSHOT_BYTES", 1):
            with self.assertRaisesRegex(ValueError, "storage bound"):
                await service.save_publication_draft(session, SimpleNamespace(job_id="job"), GraphPublicationCandidate("{}", "a"*64))
        session.execute.assert_not_awaited()

    async def test_missing_or_lost_draft_does_not_restore(self):
        service = GraphContinuationService()
        for sources in (None, (SimpleNamespace(), ())):
            session = self.session()
            with patch.object(service, "_publication_sources", AsyncMock(return_value=sources)), \
                 patch("app.services.graph_continuation_service.restore_graph_publication") as restore:
                self.assertIsNone(await service.load_publication_draft(session, SimpleNamespace(job_id="job")))
            restore.assert_not_called()

    async def test_saved_payload_is_revalidated_against_current_sources(self):
        service = GraphContinuationService()
        session = self.session(SimpleNamespace(payload_json="{}", payload_sha256="a"*64))
        record = SimpleNamespace(upload_id="upload", course_uuid="course", task_id="task", original_filename="source.pdf")
        with patch.object(service, "_publication_sources", AsyncMock(return_value=(record, ("persisted",)))), \
             patch("app.services.graph_continuation_service.restore_graph_publication", side_effect=ValueError("corrupt")) as restore:
            with self.assertRaisesRegex(ValueError, "corrupt"):
                await service.load_publication_draft(session, SimpleNamespace(job_id="job"))
        self.assertEqual(restore.call_args.args[1], ("persisted",))
        session.commit.assert_not_awaited()
        session.rollback.assert_not_awaited()
