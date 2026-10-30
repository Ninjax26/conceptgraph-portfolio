"""Runtime idle-slot scheduling contracts; providers/stores are deliberately absent."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from app.core.config import Settings
from app.core.processing_coordinator import ProcessingCoordinator
from app.services.graph_job_extraction import GraphJobDeferred, GraphJobAuthorityLost


class IdleGraphDispatchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = Settings(_env_file=None, GRAPH_CONTINUATION_DISPATCH_ENABLED=True,
            GRAPH_CONTINUATION_SNAPSHOTS_ENABLED=True, GRAPH_PROJECTION_FENCING_ENABLED=True,
            SHARED_PROVIDER_LIMITS_ENABLED=True, PROCESSING_ROLE='worker',
            PROVIDER_INTERACTIVE_REQUEST_RESERVE_PER_MINUTE=1,
            PROVIDER_INTERACTIVE_REQUEST_RESERVE_PER_DAY=1)
        self.config.processing_dispatch_interval_seconds = .01
        self.opens = 0
        @asynccontextmanager
        async def sessions():
            self.opens += 1
            yield SimpleNamespace()
        self.sessions = sessions
        self.claim = object()
        self.jobs = SimpleNamespace(claim_next=AsyncMock(return_value=self.claim))
        self.graph = SimpleNamespace(jobs=self.jobs, process=AsyncMock(return_value=True))
        self.documents = SimpleNamespace(process_document=AsyncMock())
        self.uploads = SimpleNamespace(claim_for_processing=AsyncMock(return_value=object()))

    def coordinator(self):
        coordinator = ProcessingCoordinator(config=self.config, upload_service=self.uploads,
            processing_service=self.documents, graph_processor=self.graph)
        coordinator._recover_and_fill = AsyncMock()
        return coordinator

    async def wait_for(self, predicate):
        async def wait():
            while not predicate():
                await asyncio.sleep(.001)
        await asyncio.wait_for(wait(), 1)

    async def test_distinct_flag_defaults_false(self):
        self.assertIs(getattr(Settings(_env_file=None), 'graph_continuation_dispatch_enabled', None), False)

    async def test_gate_is_fail_closed_before_claiming_or_constructing_processor(self):
        coordinator = self.coordinator()
        for field, value in [('graph_continuation_dispatch_enabled', False),
                ('graph_continuation_dispatch_enabled', 1),
                ('graph_continuation_snapshots_enabled', False),
                ('graph_projection_fencing_enabled', False), ('processing_role', 'api'),
                ('shared_provider_limits_enabled', False),
                ('provider_interactive_request_reserve_per_minute', 0),
                ('provider_interactive_request_reserve_per_day', 0)]:
            before = getattr(self.config, field)
            setattr(self.config, field, value)
            self.assertFalse(await coordinator._process_due_graph_job())
            setattr(self.config, field, before)
        self.assertEqual(self.opens, 0)
        self.jobs.claim_next.assert_not_awaited()

    async def test_enabled_token_limits_require_interactive_token_headroom(self):
        coordinator = self.coordinator()
        for period in ('minute', 'day'):
            setattr(self.config, 'provider_estimated_tokens_per_' + period, 10000)
            self.assertFalse(await coordinator._process_due_graph_job())
            setattr(self.config, 'provider_interactive_token_reserve_per_' + period, 1000)
        with patch('app.core.processing_coordinator.AsyncSessionLocal', self.sessions):
            self.assertTrue(await coordinator._process_due_graph_job())
        self.assertEqual(self.graph.process.await_count, 1)

    async def test_one_claim_is_committed_before_processor_and_uses_same_worker_lease(self):
        coordinator = self.coordinator()
        with patch('app.core.processing_coordinator.AsyncSessionLocal', self.sessions):
            self.assertTrue(await coordinator._process_due_graph_job())
        self.jobs.claim_next.assert_awaited_once_with(unittest.mock.ANY,
            lease_owner=coordinator.instance_id, lease_seconds=self.config.processing_lease_seconds)
        self.graph.process.assert_awaited_once_with(self.claim,
            lease_seconds=self.config.processing_lease_seconds)

    async def test_no_due_job_does_not_invoke_processor(self):
        coordinator = self.coordinator()
        self.jobs.claim_next.return_value = None
        with patch('app.core.processing_coordinator.AsyncSessionLocal', self.sessions):
            self.assertFalse(await coordinator._process_due_graph_job())
        self.graph.process.assert_not_awaited()

    async def test_queued_document_wins_before_any_new_graph_claim(self):
        coordinator = self.coordinator()
        coordinator._started = True
        await coordinator.submit('upload', 'task')
        with patch('app.core.processing_coordinator.AsyncSessionLocal', self.sessions):
            self.assertFalse(await coordinator._process_due_graph_job())
        self.assertEqual(self.opens, 0)
        self.jobs.claim_next.assert_not_awaited()

    async def test_expected_deferral_and_authority_loss_are_single_attempts(self):
        coordinator = self.coordinator()
        for error in (GraphJobDeferred('persisted wait'), GraphJobAuthorityLost('stale')):
            self.graph.process.side_effect = error
            with patch('app.core.processing_coordinator.AsyncSessionLocal', self.sessions):
                self.assertFalse(await coordinator._process_due_graph_job())
        self.assertEqual(self.graph.process.await_count, 2)
        self.assertEqual(self.jobs.claim_next.await_count, 2)

    async def test_unexpected_error_propagates_once_for_worker_logging(self):
        coordinator = self.coordinator()
        self.graph.process.side_effect = OSError('ambiguous')
        with patch('app.core.processing_coordinator.AsyncSessionLocal', self.sessions):
            with self.assertRaisesRegex(OSError, 'ambiguous'):
                await coordinator._process_due_graph_job()
        self.assertEqual(self.graph.process.await_count, 1)

    async def test_idle_timeout_shares_worker_capacity_and_preserves_queue_bookkeeping(self):
        self.config.processing_concurrency = 2
        coordinator = self.coordinator()
        entered, release = asyncio.Event(), asyncio.Event()
        running, peak = 0, 0
        async def document(*args, **kwargs):
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            if running == 2:
                entered.set()
            try:
                await release.wait()
            finally:
                running -= 1
        self.documents.process_document.side_effect = document
        with patch('app.core.processing_coordinator.AsyncSessionLocal', self.sessions):
            await coordinator.start()
            try:
                await coordinator.submit('one', 'task1')
                await coordinator.submit('two', 'task2')
                await asyncio.wait_for(entered.wait(), 1)
                await asyncio.sleep(.04)
                self.jobs.claim_next.assert_not_awaited()
                release.set()
                await asyncio.wait_for(coordinator.queue.join(), 1)
                await self.wait_for(lambda: self.graph.process.await_count > 0)
                self.assertEqual(len(coordinator._workers), 2)
                self.assertEqual(peak, 2)
                self.assertEqual(coordinator.queue_depth, 0)
                self.assertEqual(coordinator._queued, set())
            finally:
                release.set()
                await coordinator.stop()
        self.assertFalse(coordinator.started)

    async def test_repeated_stop_cancellation_drains_claim_without_starting_processor(self):
        coordinator = self.coordinator()
        entered, release = asyncio.Event(), asyncio.Event()
        async def claim(*args, **kwargs):
            entered.set()
            await release.wait()
            return self.claim
        self.jobs.claim_next.side_effect = claim
        with patch('app.core.processing_coordinator.AsyncSessionLocal', self.sessions):
            await coordinator.start()
            await asyncio.wait_for(entered.wait(), 1)
            stopping = asyncio.create_task(coordinator.stop())
            await asyncio.sleep(0)
            for worker in coordinator._workers:
                worker.cancel()
            await asyncio.sleep(0)
            self.assertFalse(stopping.done())
            release.set()
            await asyncio.wait_for(stopping, 1)
        self.graph.process.assert_not_awaited()
        self.assertEqual(coordinator._workers, [])
        self.assertFalse(coordinator.started)

    async def test_default_off_worker_does_not_poll_graphs_and_can_restart(self):
        self.config.graph_continuation_dispatch_enabled = False
        coordinator = self.coordinator()
        for _ in range(2):
            await coordinator.start()
            await asyncio.sleep(.03)
            await coordinator.stop()
        self.jobs.claim_next.assert_not_awaited()
        self.assertEqual(self.opens, 0)
