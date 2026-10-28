"""A single failed removal must not starve the rest of a bounded sweep."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from app.core.config import Settings
from app.services.demo_retention_service import DemoRetentionService


class DeletionSweepIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, failure, *, fail_open=False):
        deleted, remote = [], []
        pending = {"first", "second"}
        opened = 0

        @asynccontextmanager
        async def sessions():
            nonlocal opened
            opened += 1
            if fail_open and opened == 2:
                raise failure
            yield SimpleNamespace(rollback=AsyncMock())

        class Uploads:
            async def list_pending_deletion_ids(self, session, *, limit):
                return ["first", "second"]

            async def lock_for_deletion(self, session, upload_id):
                if upload_id == "first" and not fail_open:
                    raise failure
                return SimpleNamespace(upload_id=upload_id, course_uuid="course",
                    course_id="COURSE", storage_key=upload_id)

            async def storage_key_is_shared(self, session, key, upload_id):
                return False

            async def delete_document(self, session, upload_id):
                deleted.append(upload_id)
                pending.remove(upload_id)
                return upload_id

        class Storage:
            def delete(self, key):
                remote.append(("source", key))

        async def cleanup(upload_id, course_id):
            remote.append(("graph_vectors", upload_id))

        service = DemoRetentionService(config=Settings(_env_file=None),
            upload_service=Uploads(), storage=Storage())
        service.ingestion_service = SimpleNamespace(cleanup_upload=cleanup)
        with patch("app.services.demo_retention_service.AsyncSessionLocal", sessions):
            count = await service.resume_pending_deletions_once()
        return count, deleted, pending, remote

    async def test_failed_lock_keeps_first_pending_and_removes_later_document(self):
        with self.assertLogs("app.services.demo_retention_service", level="ERROR"):
            result = await self.exercise(RuntimeError("synthetic row-lock failure"))
        self.assertEqual(result, (1, ["second"], {"first"},
            [("graph_vectors", "second"), ("source", "second")]))

    async def test_failed_session_open_does_not_skip_later_document(self):
        with self.assertLogs("app.services.demo_retention_service", level="ERROR"):
            result = await self.exercise(RuntimeError("synthetic connection failure"), fail_open=True)
        self.assertEqual(result, (1, ["second"], {"first"},
            [("graph_vectors", "second"), ("source", "second")]))

    async def test_cancellation_during_lock_stops_sweep(self):
        with self.assertRaises(asyncio.CancelledError):
            await self.exercise(asyncio.CancelledError())
