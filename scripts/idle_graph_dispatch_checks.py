"""Owned real-store idle coordinator checks; authored graphs, no hosted providers."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import func, select

from app.core import processing_coordinator as coordination
from app.core.config import Settings
from app.models.document_upload import DocumentUpload, GraphContinuationJob, GraphPublicationDraft


async def qualify_idle_dispatch(sessions, service, authored, prepared, inspect, completed, checks, observations):
    cfg = Settings(_env_file=None, GRAPH_CONTINUATION_DISPATCH_ENABLED=True,
        GRAPH_CONTINUATION_SNAPSHOTS_ENABLED=True, GRAPH_PROJECTION_FENCING_ENABLED=True,
        SHARED_PROVIDER_LIMITS_ENABLED=True, PROCESSING_ROLE='worker',
        PROVIDER_INTERACTIVE_REQUEST_RESERVE_PER_MINUTE=1,
        PROVIDER_INTERACTIVE_REQUEST_RESERVE_PER_DAY=1,
        PROCESSING_DISPATCH_INTERVAL_SECONDS=.25)

    def coordinator(ingestion=authored):
        return coordination.ProcessingCoordinator(config=cfg,
            processing_service=SimpleNamespace(ingestion_service=ingestion))

    async def requeue(claim):
        # Fixture setup only: expose the prepared job to real claim_next, not a
        # scheduler double. The original document READY/task remains untouched.
        async with sessions() as session:
            job = await session.get(GraphContinuationJob, claim.job_id)
            job.status, job.attempt_count = 'pending', 0
            job.lease_owner = job.claim_token = job.lease_expires_at = None
            job.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            await session.commit()

    async def wait_status(claim, expected):
        async def wait():
            while True:
                async with sessions() as session:
                    job = await session.get(GraphContinuationJob, claim.job_id)
                    if job.status == expected and (expected != 'pending' or job.failure_category == 'provider_quota'):
                        return
                await asyncio.sleep(.02)
        await asyncio.wait_for(wait(), 20)

    with patch.object(coordination, 'AsyncSessionLocal', sessions):
        authored.limited = False
        claim = await prepared()
        await requeue(claim)
        before = authored.calls
        workers = [coordinator(), coordinator()]
        try:
            await asyncio.gather(*(worker.start() for worker in workers))
            await wait_status(claim, 'completed')
        finally:
            await asyncio.gather(*(worker.stop() for worker in workers))
        completed(await inspect(claim))
        async with sessions() as session:
            job = await session.get(GraphContinuationJob, claim.job_id)
            assert job.attempt_count == 1
        assert authored.calls == before
        assert all(not worker.started and not worker._workers for worker in workers)
        checks['idle_competing_coordinators_claim_saved_draft_once_without_extraction'] = True

        claim = await prepared(save_draft=False)
        await requeue(claim)
        worker = coordinator()
        before = authored.calls
        try:
            await worker.start()
            await wait_status(claim, 'completed')
        finally:
            await worker.stop()
        completed(await inspect(claim))
        assert authored.calls == before + 1
        checks['idle_worker_extracts_authoritative_snapshot_and_completes_real_projection'] = True

        claim = await prepared(save_draft=False)
        await requeue(claim)
        before_state, before_calls = await inspect(claim), authored.calls
        authored.limited = True
        worker = coordinator()
        try:
            await worker.start()
            await wait_status(claim, 'pending')
            # Several actual poll intervals must not bypass the persisted reset.
            await asyncio.sleep(.8)
            async with sessions() as session:
                job = await session.get(GraphContinuationJob, claim.job_id)
                now = (await session.execute(select(func.clock_timestamp()))).scalar_one()
                assert job.attempt_count == 1 and job.next_attempt_at > now
                assert job.claim_token is None and job.lease_owner is None
                assert await session.get(GraphPublicationDraft, claim.job_id) is None
            value = await inspect(claim)
            assert value['source_task_unchanged'] and value['source_status'] == 'ready'
            assert value['names'] == before_state['names']
            assert value['intents'] == value['receipts'] == value['markers'] == 0
            assert authored.calls == before_calls + 1
        finally:
            await worker.stop()
            authored.limited = False
        checks['idle_worker_respects_durable_quota_deadline_across_multiple_polls'] = True

        claim = await prepared()
        await requeue(claim)
        entered, release = asyncio.Event(), asyncio.Event()
        class SlowPublication(type(authored)):
            async def replace_graph_extraction(self, *args, **kwargs):
                await super().replace_graph_extraction(*args, **kwargs)
                entered.set()
                await release.wait()
        slow = SlowPublication(graph_driver=authored.graph_driver, vector_client=SimpleNamespace())
        worker = coordinator(slow)
        stopping = None
        try:
            await worker.start()
            await asyncio.wait_for(entered.wait(), 20)
            stopping = asyncio.create_task(worker.stop())
            await asyncio.sleep(.02)
            for task in worker._workers:
                task.cancel()
            await asyncio.sleep(.02)
            assert not stopping.done()
            async with sessions() as session:
                locked = (await session.execute(select(DocumentUpload.upload_id).where(
                    DocumentUpload.upload_id == claim.upload_id).with_for_update(skip_locked=True))).scalar_one_or_none()
                assert locked is None
            release.set()
            await asyncio.wait_for(stopping, 20)
            completed(await inspect(claim))
            assert not worker.started and not worker._workers and worker.queue_depth == 0
        finally:
            release.set()
            if stopping is not None:
                await asyncio.wait_for(stopping, 20)
            else:
                await worker.stop()
        checks['idle_worker_shutdown_drains_remote_receipt_unit_before_releasing_source_lock'] = True
        observations['idle_graph_dispatch'] = {
            'actual_coordinators_same_event_loop': True, 'separate_os_workers': False,
            'competing_coordinators': 2, 'per_coordinator_concurrency': 1,
            'saved_draft_extraction_calls': 0, 'fresh_authored_extraction_calls': 1,
            'quota_authored_extraction_calls': 1, 'quota_observation_seconds': .8,
            'quota_reclaims_before_reset': 0, 'shutdown_held_document_lock': True,
            'authored_extraction_no_provider_admission_or_inference': True,
            'document_queue_capacity_tests': 'offline unit suite; no four-store ingestion in this fixture',
            'exhausted_recovery_and_terminal_retention_scheduled': False,
            'production_flags_activated': False}
