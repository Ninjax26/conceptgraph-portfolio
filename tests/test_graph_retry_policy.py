from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import HTTPException
from groq import RateLimitError

from app.api.endpoints import ingest
from app.core.exceptions import LLMProviderRateLimitError, SharedProviderBudgetExceeded
from app.schemas.extraction import ConceptNode, GraphExtractionResponse
from app.services.graph_retry_policy import (
    GraphRetryNotDue, MAX_GRAPH_RETRY_DELAY, quota_retry_delay, retry_not_before, retry_wait_seconds,
)
from app.services.ingestion_service import IngestionService
from app.services.parser_service import DocumentChunk


class GraphRetryPolicyTests(unittest.TestCase):
    def test_local_budget_reset_is_not_extended_by_vendor_cooldown(self):
        self.assertEqual(quota_retry_delay(SharedProviderBudgetExceeded("local", retry_after_seconds=61)), 61)

    def test_vendor_delay_respects_both_reset_and_circuit_minimum(self):
        with patch("app.services.graph_retry_policy.settings.llm_failover_cooldown_seconds", 300):
            self.assertEqual(quota_retry_delay(LLMProviderRateLimitError("quota", retry_after_seconds=61)), 300)
            self.assertEqual(quota_retry_delay(LLMProviderRateLimitError("quota", retry_after_seconds=7200)), 7200)

    def test_sdk_retry_after_header_is_used(self):
        error = RateLimitError("quota", response=httpx.Response(429, headers={"Retry-After":"3600"},
            request=httpx.Request("POST", "https://example.test")), body=None)
        self.assertEqual(quota_retry_delay(error), 3600)

    def test_invalid_resets_use_safe_default_and_huge_resets_are_bounded(self):
        with patch("app.services.graph_retry_policy.settings.llm_failover_cooldown_seconds", 300):
            for value in (None, float("nan"), float("inf"), -2, "invalid"):
                self.assertEqual(quota_retry_delay(LLMProviderRateLimitError("quota", retry_after_seconds=value)), 300)
        self.assertEqual(quota_retry_delay(LLMProviderRateLimitError("quota", retry_after_seconds=10**20)), MAX_GRAPH_RETRY_DELAY)

    def test_deadline_roundtrip_rounds_remaining_wait_up(self):
        now = datetime(2026,10,13,tzinfo=timezone.utc)
        deadline = retry_not_before(60.2, now)
        self.assertEqual(retry_wait_seconds({"graph_retry_not_before":deadline}, now), 61)
        self.assertEqual(retry_wait_seconds({"graph_retry_not_before":deadline}, now+timedelta(seconds=61)), 0)

    def test_invalid_or_legacy_metadata_does_not_permanently_block_retry(self):
        now = datetime.now(timezone.utc)
        for result in (None, {}, {"graph_retry_not_before":42}, {"graph_retry_not_before":"invalid"},
                       {"graph_retry_not_before":"2026-10-14T12:00:00"}):
            self.assertEqual(retry_wait_seconds(result, now), 0)
        for delay in (None, True, -1, 0, float("nan"), float("inf"), "60"):
            self.assertIsNone(retry_not_before(delay, now))

    def test_offset_timestamp_is_compared_as_an_instant(self):
        now = datetime(2026,10,13,tzinfo=timezone.utc)
        self.assertEqual(retry_wait_seconds({"graph_retry_not_before":"2026-10-13T05:31:00+05:30"}, now), 60)


class GraphRetryBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_fenced_locked_reads_refresh_existing_session_objects(self):
        from app.services.upload_service import UploadService
        session = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda:None)),
                                  rollback=AsyncMock())
        service = UploadService()
        calls = [
            lambda:service._get_current_upload(session, "fixture", "task", lease_owner="worker"),
            lambda:service.claim_for_processing(session, upload_id="fixture", task_id="task", lease_owner="worker", lease_seconds=180),
            lambda:service.lock_for_deletion(session, "fixture"),
        ]
        for call in calls:
            await call()
            self.assertTrue(session.execute.call_args.args[0].get_execution_options()["populate_existing"])

    async def test_api_returns_retry_after_without_storage_or_enqueue_work(self):
        record = SimpleNamespace(stage="READY", graph_status="GRAPH_PARTIAL", attempt_count=1,
                                 graph_node_count=2, result_json={"graph_batches_failed":1})
        with patch.object(ingest.upload_service, "get_upload", AsyncMock(return_value=record)), \
             patch.object(ingest.upload_service, "retry_upload", AsyncMock(side_effect=GraphRetryNotDue(61))), \
             patch.object(ingest.storage_service, "exists") as exists, \
             patch.object(ingest.processing_coordinator, "submit", AsyncMock()) as enqueue:
            with self.assertRaises(HTTPException) as raised:
                await ingest.retry_upload("fixture", SimpleNamespace())
        self.assertEqual(raised.exception.status_code, 429)
        self.assertEqual(raised.exception.headers, {"Retry-After":"61"})
        exists.assert_not_called()
        enqueue.assert_not_awaited()

    async def test_quota_resume_reuses_completed_batches_and_clears_old_delay(self):
        chunks = [DocumentChunk(id=f"c{index}", text=f"Lesson {index}", metadata={
            "upload_id":"fixture", "document_name":"fixture.pdf", "page_number":index+1,
            "section_heading":f"Section {index}"}) for index in range(3)]
        def graph(index):
            return GraphExtractionResponse(nodes=[ConceptNode(id=f"n{index}", name=f"Concept {index}",
                type="concept", source_chunk_id=f"c{index}")])
        checkpoints = {}
        async def save(batch, extraction):
            checkpoints[batch.batch_key] = extraction
        service = IngestionService(SimpleNamespace(), SimpleNamespace())
        service.extract_graph_from_text = AsyncMock(side_effect=[graph(0), SharedProviderBudgetExceeded("holdback", retry_after_seconds=61)])
        with patch("app.services.ingestion_service.settings.graph_global_linking_enabled", False):
            first = await service.extract_graph_from_chunks(chunks, on_batch_completed=save)
            self.assertEqual(first.provider_retry_after_seconds, 61)
            self.assertEqual(len(checkpoints), 1)
            self.assertEqual(service.extract_graph_from_text.await_count, 2)
            service.extract_graph_from_text = AsyncMock(side_effect=[graph(1), graph(2)])
            resumed = await service.extract_graph_from_chunks(chunks, completed_batches=checkpoints, on_batch_completed=save)
        self.assertEqual(service.extract_graph_from_text.await_count, 2)
        self.assertEqual(resumed.batches_succeeded, 3)
        self.assertEqual(len(checkpoints), 3)
        self.assertIsNone(resumed.provider_retry_after_seconds)
        self.assertFalse(resumed.provider_limited)

    async def test_global_linking_quota_is_not_silently_lost(self):
        chunk = DocumentChunk(id="c0", text="Alpha depends on Beta", metadata={
            "upload_id":"fixture", "document_name":"fixture.pdf", "page_number":1, "section_heading":"Lesson"})
        section = GraphExtractionResponse(nodes=[ConceptNode(id=name, name=name, type="concept", source_chunk_id="c0")
                                                for name in ("Alpha", "Beta")])
        service = IngestionService(SimpleNamespace(), SimpleNamespace())
        service.extract_graph_from_text = AsyncMock(side_effect=[section, SharedProviderBudgetExceeded("holdback", retry_after_seconds=120)])
        with patch("app.services.ingestion_service.settings.graph_global_linking_enabled", True):
            result = await service.extract_graph_from_chunks([chunk])
        self.assertTrue(result.provider_limited)
        self.assertEqual(result.provider_retry_after_seconds, 120)
        self.assertFalse(result.global_linking_processed)
        self.assertEqual(len(result.nodes), 2)
