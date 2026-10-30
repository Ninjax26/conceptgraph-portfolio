"""One-claim graph processing contracts; no providers, stores or source/vector writes."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from app.schemas.extraction import GraphExtractionResponse
from app.services.graph_job_extraction import GraphJobAuthorityLost, GraphJobDeferred
from app.services.graph_only_processor import GraphOnlyProcessor, GraphJobNotPublishable


class GraphOnlyProcessorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.events, self.open_sessions = [], 0
        @asynccontextmanager
        async def sessions():
            self.open_sessions += 1
            self.events.append('open')
            try:
                yield SimpleNamespace(commit=AsyncMock(side_effect=lambda: self.events.append('commit')),
                    rollback=AsyncMock(side_effect=lambda: self.events.append('rollback')))
            finally:
                self.open_sessions -= 1
                self.events.append('close')
        self.sessions = sessions
        self.config = SimpleNamespace(graph_projection_fencing_enabled=True,
            graph_continuation_snapshots_enabled=True, processing_role='worker')
        self.candidate = object()
        self.graph = GraphExtractionResponse()
        self.jobs = SimpleNamespace(heartbeat=AsyncMock(return_value=True),
            load_publication_draft=AsyncMock(return_value=None),
            prepare_publication=AsyncMock(side_effect=lambda *a: self.event('prepare', self.candidate)),
            save_publication_draft=AsyncMock(side_effect=lambda *a: self.event('save', True)),
            publish_publication=AsyncMock(side_effect=self.publish))
        self.extractor = SimpleNamespace(extract=AsyncMock(return_value=self.graph))
        self.budgets = SimpleNamespace(lock=AsyncMock(side_effect=lambda *a: self.event('admission', None)))
        self.ingestion = object()  # No parser, encoder, vector or source-storage interface.
        self.claim = object()
        self.runner = GraphOnlyProcessor(self.sessions, self.ingestion, config=self.config,
            jobs=self.jobs, extractor=self.extractor, budgets=self.budgets)

    def event(self, name, result):
        self.events.append(name)
        return result

    async def publish(self, *args):
        self.assertEqual(self.open_sessions, 0, 'Never retain orchestration sessions across remote publication')
        self.events.append('publish')
        return True

    async def test_fresh_output_stages_under_admission_then_publishes_after_commit_and_close(self):
        self.assertTrue(await self.runner.process(self.claim))
        self.extractor.extract.assert_awaited_once_with(self.claim, lease_seconds=180)
        self.jobs.prepare_publication.assert_awaited_once()
        self.jobs.publish_publication.assert_awaited_once_with(self.sessions, self.claim, self.ingestion)
        self.assertEqual(self.events, ['open','rollback','close','open','admission','prepare','save','commit','close','publish'])

    async def test_saved_validated_draft_skips_extraction_and_draft_replacement(self):
        self.jobs.load_publication_draft.return_value = self.candidate
        self.assertTrue(await self.runner.process(self.claim))
        self.extractor.extract.assert_not_awaited()
        self.jobs.prepare_publication.assert_not_awaited()
        self.budgets.lock.assert_not_awaited()
        self.assertEqual(self.events, ['open','rollback','close','publish'])

    async def test_default_off_role_and_strict_boolean_gates_precede_all_io(self):
        for field, value in [('graph_projection_fencing_enabled',False),
                ('graph_continuation_snapshots_enabled',False), ('processing_role','api'),
                ('graph_projection_fencing_enabled',1), ('graph_continuation_snapshots_enabled','true')]:
            before = getattr(self.config, field)
            setattr(self.config, field, value)
            with self.assertRaises(RuntimeError):
                await self.runner.process(self.claim)
            setattr(self.config, field, before)
        self.assertEqual(self.events, [])
        self.jobs.heartbeat.assert_not_awaited()
        default = GraphOnlyProcessor(self.sessions, self.ingestion, jobs=self.jobs)
        with self.assertRaises(RuntimeError):
            await default.process(self.claim)
        self.assertEqual(self.events, [])

    async def test_invalid_lease_never_opens_a_session(self):
        for value in (True,0,3601,1.5,'180'):
            with self.assertRaises(ValueError):
                await self.runner.process(self.claim, lease_seconds=value)
        self.assertEqual(self.events, [])

    async def test_initial_lost_authority_prevents_draft_read_and_extraction(self):
        self.jobs.heartbeat.return_value = False
        with self.assertRaises(GraphJobAuthorityLost):
            await self.runner.process(self.claim)
        self.jobs.load_publication_draft.assert_not_awaited()
        self.extractor.extract.assert_not_awaited()
        self.jobs.publish_publication.assert_not_awaited()
        self.assertEqual(self.open_sessions, 0)

    async def test_invalid_saved_draft_fails_closed_without_falling_back_to_provider(self):
        self.jobs.load_publication_draft.side_effect = ValueError('corrupt draft')
        with self.assertRaises(ValueError):
            await self.runner.process(self.claim)
        self.extractor.extract.assert_not_awaited()
        self.jobs.publish_publication.assert_not_awaited()
        self.assertEqual(self.events, ['open','rollback','close'])

    async def test_durable_quota_deferral_is_propagated_without_draft_or_publication(self):
        self.extractor.extract.side_effect = GraphJobDeferred('persisted deadline')
        with self.assertRaises(GraphJobDeferred):
            await self.runner.process(self.claim)
        self.jobs.prepare_publication.assert_not_awaited()
        self.jobs.publish_publication.assert_not_awaited()

    async def test_unknown_quota_deadline_never_persists_an_unpublishable_draft(self):
        self.extractor.extract.return_value = GraphExtractionResponse(provider_limited=True)
        with self.assertRaises(GraphJobNotPublishable):
            await self.runner.process(self.claim)
        self.jobs.prepare_publication.assert_not_awaited()
        self.jobs.publish_publication.assert_not_awaited()

    async def test_lost_authority_during_prepare_never_commits_or_publishes(self):
        self.jobs.prepare_publication.side_effect = None
        self.jobs.prepare_publication.return_value = None
        with self.assertRaises(GraphJobAuthorityLost):
            await self.runner.process(self.claim)
        self.jobs.save_publication_draft.assert_not_awaited()
        self.assertNotIn('commit', self.events)
        self.jobs.publish_publication.assert_not_awaited()

    async def test_capacity_or_fence_failure_does_not_commit_or_publish(self):
        self.jobs.save_publication_draft.side_effect = None
        self.jobs.save_publication_draft.return_value = False
        with self.assertRaises(GraphJobNotPublishable):
            await self.runner.process(self.claim)
        self.assertNotIn('commit', self.events)
        self.jobs.publish_publication.assert_not_awaited()
        self.assertEqual(self.open_sessions, 0)

    async def test_stale_publication_is_not_reported_as_success(self):
        self.jobs.publish_publication.side_effect = None
        self.jobs.publish_publication.return_value = False
        with self.assertRaises(GraphJobAuthorityLost):
            await self.runner.process(self.claim)
        self.assertEqual(self.jobs.publish_publication.await_count, 1)

    async def test_publication_error_is_not_immediately_retried(self):
        self.jobs.publish_publication.side_effect = RuntimeError('ambiguous acknowledgement')
        with self.assertRaisesRegex(RuntimeError, 'ambiguous'):
            await self.runner.process(self.claim)
        self.assertEqual(self.jobs.publish_publication.await_count, 1)
        self.assertEqual(self.extractor.extract.await_count, 1)

    async def test_repeated_cancellation_drains_draft_commit_and_never_starts_publish(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def save(*args):
            started.set()
            await release.wait()
            return True
        self.jobs.save_publication_draft.side_effect = save
        task = asyncio.create_task(self.runner.process(self.claim))
        await started.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        self.assertEqual(self.open_sessions, 1)
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIn('commit', self.events)
        self.assertEqual(self.open_sessions, 0)
        self.jobs.publish_publication.assert_not_awaited()
