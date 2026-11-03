"""Private graph runtime subprocesses; authored drafts, no external AI or vector I/O."""
import argparse
import asyncio
from contextlib import ExitStack
from pathlib import Path
import os
import signal
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import urlparse

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from neo4j import AsyncGraphDatabase
from app.core.config import Settings
from app.models.document_upload import GraphContinuationJob
from app.services.ingestion_service import IngestionService
from scripts.evaluate_provider_admission import engine_for
from scripts.evaluate_graph_continuation_jobs import configuration


def runtime_config(base, role):
    if role not in {'worker','combined'}:
        raise ValueError('Only an explicit private runtime role is supported.')
    return base.model_copy(update=dict(processing_role=role,shared_provider_limits_enabled=True,
        graph_projection_fencing_enabled=True,graph_continuation_snapshots_enabled=True,
        graph_continuation_dispatch_enabled=True,graph_continuation_maintenance_enabled=True,
        provider_interactive_request_reserve_per_minute=1,provider_interactive_request_reserve_per_day=1,
        processing_lease_seconds=60,processing_heartbeat_seconds=5,processing_dispatch_interval_seconds=.25))


async def child(args):
    endpoint=urlparse(args.neo4j_uri)
    if endpoint.scheme!='bolt' or endpoint.hostname!='127.0.0.1' or not endpoint.port:
        raise ValueError('Only the owned loopback Neo4j fixture is permitted.')
    engine=engine_for(args,args.schema)
    driver=AsyncGraphDatabase.driver(args.neo4j_uri,auth=None)
    from app import worker,main
    from app.core import database,processing_coordinator as coordination
    from app.services import worker_presence_service as presence,graph_continuation_service as jobs
    class DraftOnly(IngestionService):
        def validate_qdrant_collection(self): pass  # Deliberately omitted; separate four-store matrix.
        async def extract_graph_from_chunks(self,*a,**k):
            raise AssertionError('Saved-draft runtime must never call an AI provider')
        async def replace_graph_extraction(self,*a,**k):
            await super().replace_graph_extraction(*a,**k)
            if args.crash:
                os.kill(os.getpid(),signal.SIGKILL)
    try:
        sessions=async_sessionmaker(engine,expire_on_commit=False,autoflush=False)
        with ExitStack() as stack:
            configuration(stack)
            cfg=runtime_config(jobs.settings,args.role)
            ingestion=DraftOnly(graph_driver=driver,vector_client=SimpleNamespace())
            processor=SimpleNamespace(ingestion_service=ingestion)
            coordinator=coordination.ProcessingCoordinator(config=cfg,processing_service=processor)
            retention=SimpleNamespace(start=AsyncMock(),stop=AsyncMock())
            # Only graph lifecycle is under test here; actual schema, claim,
            # heartbeat, presence, projection, maintenance and stop code runs.
            for module,values in [(database,dict(postgres_engine=engine)),
                (coordination,dict(AsyncSessionLocal=sessions)),(presence,dict(AsyncSessionLocal=sessions))]:
                for name,value in values.items(): stack.enter_context(patch.object(module,name,value))
            for module in (worker,main):
                for name,value in dict(settings=cfg,processing_coordinator=coordinator,
                    document_processing_service=processor,demo_retention_service=retention,
                    close_database_connections=AsyncMock()).items():
                    stack.enter_context(patch.object(module,name,value))
            if args.role=='worker':
                await worker.run_worker()
            else:
                stopped=asyncio.Event()
                loop=asyncio.get_running_loop()
                loop.add_signal_handler(signal.SIGTERM,stopped.set)
                try:
                    async with main.lifespan(main.app): await stopped.wait()
                finally: loop.remove_signal_handler(signal.SIGTERM)
            assert not coordinator.started and not coordinator._workers
    finally:
        await driver.close()
        await engine.dispose()


async def qualify_runtime(args,schema,uri,sessions,service,prepared,inspect,completed,checks,observations):
    async def launch(role,crash=False):
        command=[sys.executable,str(Path(__file__).resolve()),'--socket-dir',str(args.socket_dir),
            '--port',str(args.port),'--schema',schema,'--neo4j-uri',uri,'--role',role]
        if crash: command.append('--crash')
        return await asyncio.create_subprocess_exec(*command,stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE)
    async def stop(proc):
        if proc.returncode is None: proc.send_signal(signal.SIGTERM)
        try:
            _,error=await asyncio.wait_for(proc.communicate(),20)
        except BaseException:
            if proc.returncode is None: proc.kill()
            await proc.wait()
            raise
        assert proc.returncode==0,error[-1500:]
    # Isolate earlier authored quota fixtures from this long natural-lease wait.
    # This changes owned test jobs only, never production or tested source data.
    async with sessions() as session:
        pending=(await session.execute(select(GraphContinuationJob).where(
            GraphContinuationJob.status=='pending'))).scalars().all()
        for job in pending:
            job.status,job.failure_category='stopped','fixture_isolation'
        await session.commit()
    for role in ('worker','combined'):
        claim=await prepared()
        async with sessions() as session:
            job=await session.get(GraphContinuationJob,claim.job_id)
            # Fixture admission only: one remaining attempt for maintenance to
            # recover after a natural 60-second final lease expiry.
            job.status='pending'; job.attempt_count=job.max_attempts-1
            job.claim_token=job.lease_owner=job.lease_expires_at=None
            await session.commit()
        crashing=await launch(role,True)
        try:
            _,error=await asyncio.wait_for(crashing.communicate(),30)
            assert crashing.returncode==-signal.SIGKILL,error[-1500:]
        finally:
            if crashing.returncode is None: crashing.kill(); await crashing.wait()
        before=await inspect(claim)
        assert before['job']=='running' and before['receipts']==0 and before['markers']==1
        assert before['source_task_unchanged'] and before['source_status']=='ready'
        restarted=await launch(role)
        try:
            async def wait_completed():
                while True:
                    async with sessions() as session:
                        job=await session.get(GraphContinuationJob,claim.job_id)
                        if job.status=='completed': return
                    if restarted.returncode is not None: raise AssertionError('Replacement runtime exited early')
                    await asyncio.sleep(.25)
            await asyncio.wait_for(wait_completed(),135)
        finally:
            await stop(restarted)
        completed(await inspect(claim))
        checks[role+'_entrypoint_sigkill_remote_commit_natural_final_lease_recovery']=True
    observations['graph_runtime']={'roles':['worker entrypoint','combined ASGI lifespan'],
        'os_processes':4,'sigkill_boundaries':'after real Neo4j commit before SQL receipt',
        'natural_lease_seconds':60,'lease_clock_modified_for_recovery':False,
        'saved_authored_drafts_no_provider_calls':True,'startup_qdrant_validation_and_retention_timer_omitted':True,
        'not_an_http_api_or_four_store_graph_worker_benchmark':True,'production_activation':False}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--socket-dir',type=Path,required=True);p.add_argument('--port',type=int,default=55432)
    p.add_argument('--schema',required=True);p.add_argument('--neo4j-uri',required=True)
    p.add_argument('--role',required=True);p.add_argument('--crash',action='store_true')
    asyncio.run(child(p.parse_args()))
