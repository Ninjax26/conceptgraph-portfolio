"""Real PostgreSQL/Neo4j maintenance qualification on owned authored fixtures."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import select

from app.models.document_upload import GraphContinuationJob, GraphPublicationDraft, GraphPublicationIntent


async def qualify_graph_maintenance(sessions, service, ingestion, prepared, inspect, completed, checks, observations):
    class Ambiguous(type(ingestion)):
        async def replace_graph_extraction(self, *args, **kwargs):
            await super().replace_graph_extraction(*args, **kwargs)
            raise OSError('authored lost acknowledgement after real remote commit')

    async def exhaust(claim):
        async with sessions() as session:
            job=await session.get(GraphContinuationJob,claim.job_id)
            job.status,job.failure_category='stopped','attempt_limit'
            job.attempt_count=job.max_attempts
            job.claim_token=job.lease_owner=job.lease_expires_at=None
            job.updated_at=datetime.now(timezone.utc)-timedelta(days=10)
            await session.commit()

    claim=await prepared()
    try:
        await service.publish_publication(sessions,claim,
            Ambiguous(graph_driver=ingestion.graph_driver,vector_client=SimpleNamespace()))
    except OSError: pass
    else: raise AssertionError('Ambiguous fixture did not commit remotely')
    await exhaust(claim)
    async with sessions() as session:
        intent=(await session.execute(select(GraphPublicationIntent).where(
            GraphPublicationIntent.job_id==claim.job_id))).scalar_one()
        intent_id=intent.id
        assert await service.prune_terminal_snapshots(session,older_than_days=7)==0, 'Unreceipted intent lost its snapshot'
        assert await service.prune_terminal_drafts(session,older_than_days=7)==0, 'Unreceipted intent lost its draft'
        await session.commit()
        job=await session.get(GraphContinuationJob,claim.job_id)
        assert job.snapshot_json and await session.get(GraphPublicationDraft,claim.job_id) is not None
    checks['maintenance_retention_preserves_old_unreceipted_intent_recovery_bodies']=True

    from app.core.config import Settings
    from app.services.graph_job_maintenance import GraphJobMaintenance
    cfg=Settings(_env_file=None,GRAPH_CONTINUATION_MAINTENANCE_ENABLED=True,
        GRAPH_CONTINUATION_DISPATCH_ENABLED=True,GRAPH_PROJECTION_FENCING_ENABLED=True,
        GRAPH_CONTINUATION_SNAPSHOTS_ENABLED=True,PROCESSING_ROLE='worker',
        SHARED_PROVIDER_LIMITS_ENABLED=True,PROVIDER_INTERACTIVE_REQUEST_RESERVE_PER_MINUTE=1,
        PROVIDER_INTERACTIVE_REQUEST_RESERVE_PER_DAY=1)
    missing=await prepared()
    async with sessions() as session:
        missing_intent=await service.stage_publication_intent(session,missing)
        missing_id=missing_intent.id
        await session.commit()
    await exhaust(missing)
    async with sessions() as session:
        ids=await service.list_exhausted_publications(session,limit=1)
        assert len(ids)==1 and ids[0] in {intent_id,missing_id}
        following=await service.list_exhausted_publications(session,limit=1,after_intent_id=ids[0])
        assert len(following)==1 and sorted(ids+following)==sorted([intent_id,missing_id])
        assert await service.list_exhausted_publications(session,limit=1,after_intent_id=following[0])==[]
    checks['maintenance_exhausted_discovery_has_bounded_stable_cursor_and_excludes_other_jobs']=True

    maintenance=GraphJobMaintenance(sessions,ingestion,config=cfg,jobs=service)
    assert await maintenance.run_if_due()
    completed(await inspect(claim))
    value=await inspect(missing)
    assert value['job']=='stopped' and value['receipts']==0 and value['markers']==0
    assert value['source_task_unchanged'] and value['source_status']=='ready'
    async with sessions() as session:
        job=await session.get(GraphContinuationJob,missing.job_id)
        assert job.snapshot_json and await session.get(GraphPublicationDraft,missing.job_id) is not None
    assert not await maintenance.run_if_due()
    checks['maintenance_recovers_exact_final_marker_and_keeps_missing_marker_stopped']=True

    # Completed evidence may be pruned only after its configured retention.
    async with sessions() as session:
        job=await session.get(GraphContinuationJob,claim.job_id)
        job.updated_at=datetime.now(timezone.utc)-timedelta(days=10)
        await session.commit()
    fresh=GraphJobMaintenance(sessions,ingestion,config=cfg,jobs=service)
    assert await fresh.run_if_due()
    async with sessions() as session:
        job=await session.get(GraphContinuationJob,claim.job_id)
        assert job.status=='completed' and job.snapshot_json=={}
        assert await session.get(GraphPublicationDraft,claim.job_id) is None
    completed(await inspect(claim))
    checks['maintenance_prunes_receipted_terminal_bodies_preserving_ready_tombstone_and_checkpoints']=True

    # Exercise actual coordinator recovery, not only construction of a sweeper.
    runtime_claim=await prepared()
    try:
        await service.publish_publication(sessions,runtime_claim,
            Ambiguous(graph_driver=ingestion.graph_driver,vector_client=SimpleNamespace()))
    except OSError: pass
    else: raise AssertionError('Runtime ambiguous fixture did not commit remotely')
    await exhaust(runtime_claim)
    from app.core import processing_coordinator as coordination
    with patch.object(coordination,'AsyncSessionLocal',sessions):
        coordinator=coordination.ProcessingCoordinator(config=cfg,
            processing_service=SimpleNamespace(ingestion_service=ingestion))
        try:
            await coordinator.start()
            async def await_sweep():
                while True:
                    async with sessions() as session:
                        job=await session.get(GraphContinuationJob,runtime_claim.job_id)
                        if job.status=='completed': return
                    await asyncio.sleep(.02)
            await asyncio.wait_for(await_sweep(),10)
        finally:
            await coordinator.stop()
        assert not coordinator.started and not coordinator._workers
        completed(await inspect(runtime_claim))
    checks['maintenance_actual_idle_worker_recovers_final_receipt_and_drains_sweeper']=True
    observations['graph_job_maintenance']={
        'actual_local_postgresql_neo4j':True,'hosted_calls':0,
        'fixture_terminal_age_days':10,'retention_days':7,
        'fixture_age_advances_not_wall_clock_retention_measurement':True,
        'remote_commit_final_attempt_recovered_without_extraction_or_rewrite':True,
        'missing_marker_stays_stopped_with_bodies_retained':True,
        'known_completion_receipt_permits_body_cleanup':True,
        'actual_coordinator_same_event_loop':True,
        'separate_os_graph_worker_hard_kill_qualified':False,
        'production_flags_activated':False}
