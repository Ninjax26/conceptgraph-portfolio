"""Durable vector deletion contracts; no database or provider calls."""
from contextlib import asynccontextmanager
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from sqlalchemy.dialects import postgresql

from app.core.config import Settings
from app.models.document_upload import VectorDeletionTombstone
from app.services.vector_cleanup_service import VectorCleanupScope, VectorCleanupService


class VectorDeletionCleanupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.scope = VectorCleanupScope.from_url("http://127.0.0.1:6333", "test_chunks")

    def test_schema_retains_only_metadata_without_cascading_foreign_keys(self):
        table = VectorDeletionTombstone.__table__
        self.assertEqual(len(table.foreign_keys), 0)
        self.assertEqual({column.name for column in table.primary_key}, {"upload_id", "task_id", "store_sha256"})
        self.assertNotIn("completed_at", table.c)
        self.assertNotIn("text", table.c)

    def test_store_binding_omits_credentials_and_distinguishes_collection_or_endpoint(self):
        scope = VectorCleanupScope.from_url("http://name:password@127.0.0.1:6333/", "test_chunks")
        self.assertEqual(scope, self.scope)
        for url, collection in (("http://127.0.0.1:6334", "test_chunks"),
                                ("http://127.0.0.1:6333", "other_chunks")):
            self.assertNotEqual(scope, VectorCleanupScope.from_url(url, collection))
        self.assertEqual(len(scope.store_sha256), 64)
        for url in ("file:///tmp/data", "http://127.0.0.1:6333?secret=value"):
            with self.assertRaises(ValueError):
                VectorCleanupScope.from_url(url, "test_chunks")

    async def test_staging_includes_current_and_historical_tasks_without_committing(self):
        session = SimpleNamespace(execute=AsyncMock(), commit=AsyncMock())
        await VectorCleanupService.stage(session, SimpleNamespace(upload_id="upload", task_id="current"), self.scope)
        statement = session.execute.await_args.args[0]
        sql = str(statement.compile(dialect=postgresql.dialect()))
        self.assertIn("processing_attempts", sql)
        self.assertIn("UNION", sql)
        self.assertIn("ON CONFLICT", sql)
        self.assertIn("current", statement.compile(dialect=postgresql.dialect()).params.values())
        session.commit.assert_not_awaited()

    async def exercise(self, *, source=False, failure=None, binding=None):
        row = VectorDeletionTombstone(upload_id="upload", task_id="old", store_sha256=self.scope.store_sha256,
            collection_name="test_chunks")
        session = SimpleNamespace(execute=AsyncMock(), commit=AsyncMock(), rollback=AsyncMock())
        # try admission lock, tombstone row, then source existence
        session.execute.side_effect = [SimpleNamespace(scalar_one=lambda: True),
            SimpleNamespace(scalar_one_or_none=lambda: row), SimpleNamespace(scalar_one=lambda: source)]
        @asynccontextmanager
        async def sessions():
            yield session
        ingestion = SimpleNamespace(collection_name="test_chunks", cleanup_vectors=Mock(side_effect=failure))
        result = await VectorCleanupService.reconcile(sessions, "upload", "old", binding or self.scope, ingestion)
        return result, row, session, ingestion

    async def test_successful_or_empty_cleanup_reschedules_without_retiring_tracking(self):
        result, row, session, ingestion = await self.exercise()
        self.assertTrue(result)
        ingestion.cleanup_vectors.assert_called_once_with("upload", execution_token="old")
        self.assertIsNotNone(row.next_attempt_at)
        self.assertIsNotNone(row.last_checked_at)
        self.assertIsNone(row.last_error_type)
        session.commit.assert_awaited_once()

    async def test_remote_failure_retains_tracking_and_reschedules(self):
        result, row, session, ingestion = await self.exercise(failure=RuntimeError("synthetic lost acknowledgement"))
        self.assertFalse(result)
        self.assertEqual(row.last_error_type, "RuntimeError")
        self.assertIsNotNone(row.next_attempt_at)
        session.commit.assert_awaited_once()

    async def test_existing_source_prevents_remote_cleanup(self):
        result, row, session, ingestion = await self.exercise(source=True)
        self.assertFalse(result)
        ingestion.cleanup_vectors.assert_not_called()

    async def test_binding_mismatch_prevents_remote_cleanup(self):
        different = VectorCleanupScope.from_url("http://127.0.0.1:6334", "test_chunks")
        result, row, session, ingestion = await self.exercise(binding=different)
        self.assertFalse(result)
        ingestion.cleanup_vectors.assert_not_called()

    async def test_default_off_retention_sweep_does_not_open_database(self):
        from app.services.demo_retention_service import DemoRetentionService
        service = DemoRetentionService(config=Settings(_env_file=None))
        with patch("app.services.demo_retention_service.AsyncSessionLocal") as sessions:
            self.assertEqual(await service.cleanup_deleted_vectors_once(), 0)
            sessions.assert_not_called()

    async def test_discovery_is_bounded_due_and_excludes_live_sources(self):
        session = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(all=lambda: [])))
        self.assertEqual(await VectorCleanupService.due(session, self.scope, limit=1), [])
        sql = str(session.execute.await_args.args[0].compile(dialect=postgresql.dialect()))
        self.assertIn("NOT (EXISTS", sql)
        self.assertIn("next_attempt_at <= now()", sql)
        self.assertIn("ORDER BY", sql)
        self.assertIn("LIMIT", sql)
        for invalid in (0, 101, True):
            with self.assertRaises(ValueError):
                await VectorCleanupService.due(session, self.scope, limit=invalid)

    async def test_deletion_commits_tracking_and_flag_together_then_reacquires_source(self):
        from app.services.upload_service import UploadService
        record = SimpleNamespace(upload_id="upload", task_id="current", stage="READY", result_json={})
        session = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: record)),
            commit=AsyncMock())
        events = []
        async def stage(*args):
            self.assertTrue(record.result_json["deletion_requested"])
            events.append("stage")
        async def commit():
            events.append("commit")
        session.commit.side_effect = commit
        with patch("app.services.resource_budget_service.resource_budget_service.lock", new_callable=AsyncMock) as lock:
            with patch.object(VectorCleanupService, "stage", new_callable=AsyncMock, side_effect=stage):
                await UploadService().lock_for_deletion(session, "upload", vector_cleanup_scope=self.scope)
        self.assertEqual(events, ["stage", "commit"])
        self.assertEqual(lock.await_count, 2)
