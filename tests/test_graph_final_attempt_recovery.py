"""Offline fail-closed gates; actual-store recovery is checked by the runner."""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from app.services.graph_continuation_service import GraphContinuationService


class FinalAttemptRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def recovery(self, service):
        method = getattr(service, "reconcile_exhausted_publication", None)
        self.assertTrue(callable(method), "Exhausted publication recovery is missing")
        return method

    async def test_invalid_intent_id_never_opens_a_session(self):
        recover = self.recovery(GraphContinuationService())
        sessions, remote = Mock(), SimpleNamespace(read_graph_publication_marker=AsyncMock())
        for intent_id in (None, "", "  "):
            with self.subTest(intent_id=intent_id), self.assertRaises(ValueError):
                await recover(sessions, intent_id, remote)
        sessions.assert_not_called()
        remote.read_graph_publication_marker.assert_not_awaited()

    async def test_missing_intent_never_contacts_remote_or_commits(self):
        recover = self.recovery(GraphContinuationService())
        result = Mock()
        result.scalar_one_or_none.return_value = None
        session = SimpleNamespace(execute=AsyncMock(return_value=result), commit=AsyncMock())

        @asynccontextmanager
        async def sessions():
            yield session

        remote = SimpleNamespace(read_graph_publication_marker=AsyncMock())
        self.assertFalse(await recover(sessions, "missing", remote))
        session.commit.assert_not_awaited()
        remote.read_graph_publication_marker.assert_not_awaited()

    async def gated(self, *, status="running", attempts=7, category=None, live=False, stale=False):
        service = GraphContinuationService()
        recover = self.recovery(service)
        now = datetime.now(timezone.utc)
        intent = SimpleNamespace(upload_id="upload", job_id="job", document_task_id="task")
        record = SimpleNamespace(task_id="new-task" if stale else "task", created_at=now)
        job = SimpleNamespace(id="job", document_task_id="task", attempt_count=attempts,
            max_attempts=7, status=status, failure_category=category,
            lease_expires_at=now + timedelta(seconds=60 if live else -60))
        saved, clock = Mock(), Mock()
        saved.scalar_one_or_none.return_value = intent
        clock.scalar_one.return_value = now
        session = SimpleNamespace(execute=AsyncMock(side_effect=[saved, clock]), commit=AsyncMock())

        @asynccontextmanager
        async def sessions():
            yield session

        remote = SimpleNamespace(read_graph_publication_marker=AsyncMock())
        with (
            patch.object(service, "_document", AsyncMock(return_value=record)),
            patch.object(service, "_job", AsyncMock(return_value=job)),
            patch.object(service, "_ready", return_value=True),
        ):
            self.assertFalse(await recover(sessions, "intent", remote))
        session.commit.assert_not_awaited()
        remote.read_graph_publication_marker.assert_not_awaited()

    async def test_active_available_pending_and_other_stopped_jobs_are_ineligible(self):
        for options in (
            {"live": True}, {"attempts": 6}, {"status": "pending"},
            {"status": "completed"}, {"status": "stopped", "category": "superseded"},
        ):
            with self.subTest(options=options):
                await self.gated(**options)

    async def test_superseded_source_is_ineligible_even_at_attempt_limit(self):
        await self.gated(stale=True, status="stopped", category="attempt_limit")
