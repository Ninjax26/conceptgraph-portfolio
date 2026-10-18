"""Logical snapshot admission policy; no database or model requests."""

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from app.services.graph_continuation_service import GraphContinuationService


class SnapshotBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_retention_never_queries_database(self):
        session = SimpleNamespace(execute=AsyncMock())
        for days, limit in ((0, 1), (True, 1), (366, 1), (1, 0), (1, 101), (1, True)):
            with self.subTest(days=days, limit=limit), self.assertRaises(ValueError):
                await GraphContinuationService().prune_terminal_snapshots(session, older_than_days=days, limit=limit)
        session.execute.assert_not_awaited()
    async def test_disabled_budget_does_not_touch_session(self):
        session = SimpleNamespace(execute=AsyncMock(), flush=AsyncMock())
        with patch("app.services.graph_continuation_service.settings",
                   SimpleNamespace(graph_snapshot_max_jobs=0, graph_snapshot_max_bytes=0)):
            self.assertTrue(await GraphContinuationService._snapshot_capacity(session, "upload", {}))
        session.execute.assert_not_awaited()
        session.flush.assert_not_awaited()

    async def test_count_and_bytes_boundaries(self):
        for max_jobs, max_bytes, expected in ((2, 100, True), (1, 100, False), (2, 99, False), (0, 100, True)):
            with self.subTest(max_jobs=max_jobs, max_bytes=max_bytes):
                usage = Mock()
                usage.one.return_value = SimpleNamespace(jobs=1, bytes=80)
                candidate = Mock()
                candidate.scalar_one.return_value = 20
                session = SimpleNamespace(execute=AsyncMock(side_effect=[None, usage, candidate]), flush=AsyncMock())
                with patch("app.services.graph_continuation_service.settings",
                           SimpleNamespace(graph_snapshot_max_jobs=max_jobs, graph_snapshot_max_bytes=max_bytes)):
                    self.assertEqual(await GraphContinuationService._snapshot_capacity(session, "upload", {}), expected)
                session.flush.assert_awaited_once()
