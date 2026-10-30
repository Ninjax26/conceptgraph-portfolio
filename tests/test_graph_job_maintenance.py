"""Maintenance cadence/lifecycle contracts; DB qualification covers SQL semantics."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from app.core.config import Settings
from app.core.processing_coordinator import ProcessingCoordinator
from app.services.graph_continuation_service import GraphContinuationService
from app.services.graph_job_maintenance import GraphJobMaintenance


class GraphJobMaintenanceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = Settings(_env_file=None, GRAPH_CONTINUATION_MAINTENANCE_ENABLED=True,
            GRAPH_CONTINUATION_DISPATCH_ENABLED=True, GRAPH_PROJECTION_FENCING_ENABLED=True,
            GRAPH_CONTINUATION_SNAPSHOTS_ENABLED=True, PROCESSING_ROLE='worker',
            SHARED_PROVIDER_LIMITS_ENABLED=True, PROVIDER_INTERACTIVE_REQUEST_RESERVE_PER_MINUTE=1,
            PROVIDER_INTERACTIVE_REQUEST_RESERVE_PER_DAY=1)
        self.events, self.open_sessions, self.now = [], 0, 1000.
        @asynccontextmanager
        async def sessions():
            self.open_sessions += 1
            try:
                yield SimpleNamespace(commit=AsyncMock(side_effect=lambda:self.events.append('commit')))
            finally:
                self.open_sessions -= 1
        self.sessions = sessions
        self.jobs = SimpleNamespace(list_exhausted_publications=AsyncMock(return_value=['00000000-0000-0000-0000-000000000001']),
            reconcile_exhausted_publication=AsyncMock(side_effect=self.reconcile),
            prune_terminal_drafts=AsyncMock(side_effect=lambda *a,**k:self.events.append('drafts')),
            prune_terminal_snapshots=AsyncMock(side_effect=lambda *a,**k:self.events.append('snapshots')))
        self.budgets = SimpleNamespace(lock=AsyncMock(side_effect=lambda *a:self.events.append('admission')))
        self.ingestion = object()
        self.runner = GraphJobMaintenance(self.sessions, self.ingestion, config=self.config,
            jobs=self.jobs, budgets=self.budgets, clock=lambda:self.now)

    async def reconcile(self, *args):
        self.assertEqual(self.open_sessions, 0)
        self.events.append('reconcile')
        return True

    async def test_default_flag_and_gates_precede_all_io(self):
        self.assertIs(Settings(_env_file=None).graph_continuation_maintenance_enabled, False)
        for name, value in [('graph_continuation_maintenance_enabled',False),
                ('graph_continuation_maintenance_enabled',1),('graph_continuation_dispatch_enabled',False),
                ('graph_projection_fencing_enabled',False),('graph_continuation_snapshots_enabled',False),
                ('processing_role','api')]:
            before=getattr(self.config,name)
            setattr(self.config,name,value)
            self.assertFalse(await self.runner.run_if_due())
            setattr(self.config,name,before)
        self.jobs.list_exhausted_publications.assert_not_awaited()

    async def test_bounded_cursor_sweep_closes_read_session_and_cleans_under_admission(self):
        self.assertTrue(await self.runner.run_if_due())
        self.assertEqual(self.events, ['reconcile','admission','drafts','snapshots','commit'])
        self.jobs.list_exhausted_publications.assert_awaited_once_with(unittest.mock.ANY, limit=10, after_intent_id=None)
        self.assertFalse(await self.runner.run_if_due())
        self.now += 60
        self.jobs.list_exhausted_publications.return_value=[]
        self.assertTrue(await self.runner.run_if_due())
        self.assertEqual(self.jobs.list_exhausted_publications.call_args.kwargs['after_intent_id'],
            '00000000-0000-0000-0000-000000000001')
        self.now += 60
        await self.runner.run_if_due()
        self.assertIsNone(self.jobs.list_exhausted_publications.call_args.kwargs['after_intent_id'])

    async def test_failed_marker_does_not_stop_other_reconciliation_or_cleanup(self):
        self.jobs.list_exhausted_publications.return_value=['bad','good']
        self.jobs.reconcile_exhausted_publication.side_effect=[ValueError('conflict'),True]
        with self.assertLogs('app.services.graph_job_maintenance',level='ERROR'):
            self.assertTrue(await self.runner.run_if_due())
        self.assertEqual(self.jobs.reconcile_exhausted_publication.await_count,2)
        self.assertEqual(self.events,['admission','drafts','snapshots','commit'])

    async def test_concurrent_sweep_is_skipped_and_failure_is_not_hot_retried(self):
        entered, release=asyncio.Event(),asyncio.Event()
        async def discover(*args,**kwargs):
            entered.set()
            await release.wait()
            raise OSError('offline store')
        self.jobs.list_exhausted_publications.side_effect=discover
        task=asyncio.create_task(self.runner.run_if_due())
        await entered.wait()
        self.assertFalse(await self.runner.run_if_due())
        release.set()
        with self.assertRaises(OSError): await task
        self.assertFalse(await self.runner.run_if_due())
        self.jobs.prune_terminal_drafts.assert_not_awaited()

    async def test_repeated_cancellation_drains_cleanup_transaction(self):
        entered,release=asyncio.Event(),asyncio.Event()
        async def drafts(*args,**kwargs):
            entered.set()
            await release.wait()
            self.events.append('drafts')
        self.jobs.prune_terminal_drafts.side_effect=drafts
        task=asyncio.create_task(self.runner.run_if_due())
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        self.assertEqual(self.open_sessions,1)
        release.set()
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertEqual(self.open_sessions,0)
        self.assertEqual(self.events[-4:],['admission','drafts','snapshots','commit'])

    async def test_discovery_rejects_invalid_bounds_and_cursor_before_sql(self):
        service,session=GraphContinuationService(),SimpleNamespace(execute=AsyncMock())
        for kwargs in ({'limit':True},{'limit':0},{'limit':101},{'after_intent_id':'bad'}):
            with self.assertRaises(ValueError):
                await service.list_exhausted_publications(session,**kwargs)
        session.execute.assert_not_awaited()

    async def test_runtime_runs_maintenance_in_idle_slot_and_rechecks_arriving_document(self):
        graph=SimpleNamespace(jobs=SimpleNamespace(claim_next=AsyncMock(return_value=None)))
        coordinator=ProcessingCoordinator(config=self.config,processing_service=SimpleNamespace(),graph_processor=graph)
        coordinator._started=True
        async def maintain(): await coordinator.submit('upload','task')
        coordinator._graph_maintenance=SimpleNamespace(run_if_due=AsyncMock(side_effect=maintain))
        self.assertFalse(await coordinator._process_due_graph_job())
        coordinator._graph_maintenance.run_if_due.assert_awaited_once()
        graph.jobs.claim_next.assert_not_awaited()
