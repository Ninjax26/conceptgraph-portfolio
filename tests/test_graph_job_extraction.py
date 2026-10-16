"""Offline graph-only orchestration tests; no provider or database calls."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from app.schemas.extraction import GraphExtractionResponse
from app.services.graph_job_extraction import GraphJobAuthorityLost, GraphJobDeferred, GraphJobExtraction


class GraphJobExtractionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        @asynccontextmanager
        async def sessions():
            yield object()
        self.inputs = SimpleNamespace(chunks=("persisted",), checkpoints={"valid": "saved"})
        self.jobs = SimpleNamespace(load_input=AsyncMock(return_value=self.inputs),
            heartbeat=AsyncMock(return_value=True), save_checkpoint=AsyncMock(return_value=True),
            defer=AsyncMock(return_value=True))
        self.result = GraphExtractionResponse()
        self.ingestion = SimpleNamespace(extract_graph_from_chunks=AsyncMock(return_value=self.result))
        self.runner = GraphJobExtraction(sessions, self.ingestion, jobs=self.jobs)
        self.claim = SimpleNamespace(chunks=("untrusted",))

    async def test_authoritative_resume_and_fenced_checkpoint_callback(self):
        self.assertIs(await self.runner.extract(self.claim), self.result)
        args = self.ingestion.extract_graph_from_chunks.call_args
        self.assertEqual(args.args, (self.inputs.chunks,))
        self.assertIs(args.kwargs["completed_batches"], self.inputs.checkpoints)
        await args.kwargs["on_batch_completed"]("batch", self.result)
        self.jobs.save_checkpoint.assert_awaited_once()
        self.assertEqual(self.jobs.heartbeat.await_count, 2)

    async def test_lost_input_or_initial_lease_never_starts_extraction(self):
        self.jobs.load_input.return_value = None
        with self.assertRaises(GraphJobAuthorityLost):
            await self.runner.extract(self.claim)
        self.jobs.load_input.return_value = self.inputs
        self.jobs.heartbeat.return_value = False
        with self.assertRaises(GraphJobAuthorityLost):
            await self.runner.extract(self.claim)
        self.ingestion.extract_graph_from_chunks.assert_not_awaited()

    async def test_final_fence_failure_does_not_return_graph(self):
        self.jobs.heartbeat.side_effect = [True, False]
        with self.assertRaises(GraphJobAuthorityLost):
            await self.runner.extract(self.claim)

    async def test_known_quota_deadline_defers_once_without_returning_graph(self):
        self.ingestion.extract_graph_from_chunks.return_value = GraphExtractionResponse(
            provider_limited=True, provider_retry_after_seconds=60)
        with self.assertRaises(GraphJobDeferred):
            await self.runner.extract(self.claim)
        self.jobs.defer.assert_awaited_once()
        self.assertEqual(self.jobs.defer.call_args.kwargs["retry_after_seconds"], 60)
        self.assertEqual(self.ingestion.extract_graph_from_chunks.await_count, 1)

    async def test_quota_deferral_lost_fence_is_not_reported_as_scheduled(self):
        self.ingestion.extract_graph_from_chunks.return_value = GraphExtractionResponse(
            provider_limited=True, provider_retry_after_seconds=60)
        self.jobs.defer.return_value = False
        with self.assertRaises(GraphJobAuthorityLost):
            await self.runner.extract(self.claim)

    async def test_unknown_reset_does_not_invent_deadline(self):
        partial = GraphExtractionResponse(provider_limited=True)
        self.ingestion.extract_graph_from_chunks.return_value = partial
        self.assertIs(await self.runner.extract(self.claim), partial)
        self.jobs.defer.assert_not_awaited()

    async def test_delay_without_quota_flag_does_not_defer(self):
        self.ingestion.extract_graph_from_chunks.return_value = GraphExtractionResponse(
            provider_retry_after_seconds=60)
        await self.runner.extract(self.claim)
        self.jobs.defer.assert_not_awaited()

    async def test_cancellation_waits_for_started_deferral_transaction(self):
        started, release, committed = (asyncio.Event() for _ in range(3))
        async def defer(*args, **kwargs):
            started.set()
            await release.wait()
            committed.set()
            return True
        self.jobs.defer.side_effect = defer
        self.ingestion.extract_graph_from_chunks.return_value = GraphExtractionResponse(
            provider_limited=True, provider_retry_after_seconds=60)
        task = asyncio.create_task(self.runner.extract(self.claim))
        await started.wait()
        task.cancel()
        await asyncio.sleep(0)
        try:
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(committed.is_set())
        finally:
            release.set()

    async def test_checkpoint_rejection_propagates(self):
        self.jobs.save_checkpoint.return_value = False
        async def extraction(*args, on_batch_completed, **kwargs):
            await on_batch_completed("batch", self.result)
        self.ingestion.extract_graph_from_chunks.side_effect = extraction
        with self.assertRaises(GraphJobAuthorityLost):
            await self.runner.extract(self.claim)

    async def test_invalid_lease_never_reads_or_extracts(self):
        for lease in (True, 0, 3601, 1.5):
            with self.subTest(lease=lease), self.assertRaises(ValueError):
                await self.runner.extract(self.claim, lease_seconds=lease)
        self.jobs.load_input.assert_not_awaited()
        self.ingestion.extract_graph_from_chunks.assert_not_awaited()

    async def test_extraction_error_is_not_silently_successful(self):
        self.ingestion.extract_graph_from_chunks.side_effect = RuntimeError("fixture failure")
        with self.assertRaisesRegex(RuntimeError, "fixture failure"):
            await self.runner.extract(self.claim)
        self.jobs.save_checkpoint.assert_not_awaited()
        self.assertEqual(self.jobs.heartbeat.await_count, 1)

    async def test_cancellation_drains_extraction(self):
        started, stopped = asyncio.Event(), asyncio.Event()
        async def extraction(*args, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        self.ingestion.extract_graph_from_chunks.side_effect = extraction
        task = asyncio.create_task(self.runner.extract(self.claim))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(stopped.is_set())

    async def test_periodic_lease_failure_cancels_extraction(self):
        stopped = asyncio.Event()
        async def extraction(*args, **kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        self.ingestion.extract_graph_from_chunks.side_effect = extraction
        self.jobs.heartbeat.side_effect = [True, False]
        with self.assertRaises(GraphJobAuthorityLost):
            await asyncio.wait_for(self.runner.extract(self.claim, lease_seconds=1), timeout=3)
        self.assertTrue(stopped.is_set())

    async def test_repeated_cancellation_waits_for_checkpoint_cleanup(self):
        started, cleaning, release, stopped = (asyncio.Event() for _ in range(4))
        async def extraction(*args, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()
                stopped.set()
        self.ingestion.extract_graph_from_chunks.side_effect = extraction
        task = asyncio.create_task(self.runner.extract(self.claim))
        await started.wait()
        task.cancel()
        await cleaning.wait()
        task.cancel()
        await asyncio.sleep(0)
        try:
            self.assertFalse(task.done(), "Caller returned while extraction cleanup was active")
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(stopped.is_set())
        finally:
            release.set()
            if not task.done():
                task.cancel()
