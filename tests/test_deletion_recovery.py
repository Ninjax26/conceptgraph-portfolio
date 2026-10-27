import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException

from app.api.endpoints import ingest
from app.services.graph_continuation_service import GraphContinuationService
from app.services.upload_service import UploadService


def pending_record():
    return SimpleNamespace(upload_id="pending", task_id="task", course_uuid="course",
        course_id="COURSE", original_filename="fixture.pdf", content_hash="a"*64, storage_key="fixture.pdf",
        status="ready", stage="READY", processed_chunk_count=3, graph_node_count=3,
        graph_status="GRAPH_PARTIAL", attempt_count=1,
        result_json={"deletion_requested":True,"graph_batches_skipped":2})


class DeletionRecoveryTests(unittest.TestCase):
    def test_pending_deletion_cannot_reenter_ingestion_or_graph_publication(self):
        record = pending_record()
        self.assertFalse(UploadService.can_reprocess(record))
        self.assertFalse(GraphContinuationService._ready(record))

    def test_pending_deletion_cannot_preview_partially_deleted_source(self):
        record = pending_record()
        with (
            patch.object(ingest.upload_service, "get_upload", AsyncMock(return_value=record)),
            patch.object(ingest.storage_service, "get_bytes", Mock(return_value=b"%PDF-fixture")) as read,
        ):
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(ingest.preview_upload(record.upload_id, None, SimpleNamespace()))
        self.assertEqual(raised.exception.status_code, 409)
        read.assert_not_called()

    def test_reupload_of_document_being_removed_is_rejected_before_storage_write(self):
        from io import BytesIO
        import pymupdf
        from fastapi import UploadFile
        from starlette.datastructures import Headers

        record = pending_record()
        with pymupdf.open() as document:
            document.new_page()
            content = document.tobytes()
        file = UploadFile(filename="fixture.pdf", file=BytesIO(content),
                          headers=Headers({"content-type":"application/pdf"}))
        db = SimpleNamespace(execute=AsyncMock())
        with (
            patch.object(ingest.course_service, "get_or_create", AsyncMock(return_value=SimpleNamespace(id="course"))),
            patch.object(ingest.upload_service, "find_duplicate", AsyncMock(return_value=record)),
            patch.object(ingest.storage_service, "put_pdf") as write,
        ):
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(ingest.upload_document("COURSE", file, db))
        self.assertEqual(raised.exception.status_code, 409)
        write.assert_not_called()

    def test_explicit_deletion_recovery_starts_when_demo_expiry_is_disabled(self):
        from app.core.config import Settings
        from app.services.demo_retention_service import DemoRetentionService

        async def exercise():
            service = DemoRetentionService(config=Settings(_env_file=None, REQUIRE_UPLOAD_AUTH=False))
            visited = asyncio.Event()
            async def resume():
                visited.set()
                return 0
            service.resume_pending_deletions_once = AsyncMock(side_effect=resume)
            service.cleanup_deleted_graph_publications_once = AsyncMock(return_value=0)
            await service.start()
            try:
                await asyncio.wait_for(visited.wait(), 1)
            finally:
                await service.stop()
            service.resume_pending_deletions_once.assert_awaited_once()
        asyncio.run(exercise())
