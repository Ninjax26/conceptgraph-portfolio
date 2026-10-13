"""Offline wiring checks; generation/storage/indexing are test doubles."""

import asyncio
import unittest
from unittest.mock import patch

from app.core.config import Settings
from tests import test_processing as processing_tests


class GraphSnapshotStagingTests(unittest.TestCase):
    def exercise(self, *, enabled, limited, reset=120):
        service, uploads, ingestion, chunks = processing_tests.DocumentProcessingServiceTests()._service_fixture()
        service.config.graph_continuation_snapshots_enabled = enabled
        ingestion.extract_graph_from_chunks.return_value = ingestion.extract_graph_from_chunks.return_value.model_copy(
            update={"provider_limited": limited, "provider_retry_after_seconds": reset})
        with patch("app.services.document_processing_service.storage_service.get_bytes", return_value=b"%PDF-fixture"), \
             patch("app.services.document_processing_service.storage_service.exists", return_value=True):
            result = asyncio.run(service.process_document("upload-1", "task-1", lease_owner="instance-1"))
        self.assertEqual(result["status"], "ready")
        return uploads.mark_completed.await_args, chunks

    def test_staging_setting_defaults_off_and_accepts_explicit_opt_in(self):
        self.assertIs(Settings.model_fields["graph_continuation_snapshots_enabled"].default, False)
        cfg = Settings(_env_file=None, GRAPH_CONTINUATION_SNAPSHOTS_ENABLED=True)
        self.assertTrue(cfg.graph_continuation_snapshots_enabled)

    def test_disabled_staging_preserves_original_completion_call(self):
        call, _ = self.exercise(enabled=False, limited=True)
        self.assertEqual(call.kwargs, {"lease_owner": "instance-1"})

    def test_enabled_quota_staging_passes_exact_indexed_execution_chunks(self):
        call, chunk = self.exercise(enabled=True, limited=True)
        self.assertEqual(call.kwargs["graph_continuation_chunks"], [chunk])
        self.assertEqual(chunk.metadata["execution_token"], "task-1")
        self.assertEqual(call.args[3]["graph_retry_after_seconds"], 120)

    def test_enabled_staging_does_not_schedule_batch_budget_or_unknown_reset(self):
        for limited, reset in ((False, 120), (True, None)):
            call, _ = self.exercise(enabled=True, limited=limited, reset=reset)
            self.assertNotIn("graph_continuation_chunks", call.kwargs)
