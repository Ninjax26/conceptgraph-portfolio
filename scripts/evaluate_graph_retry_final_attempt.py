"""Isolated retry ordering probe and exhausted-attempt receipt recovery.

Real PostgreSQL/Neo4j, synthetic data, no models/Qdrant/R2/live API. An unsafe
ordinary-retry observation is an explicit activation blocker, not a passing
safety assertion. The older publication matrix is not rerun here.
"""

import argparse
import asyncio
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import signal
import sys
from types import SimpleNamespace
from urllib.parse import urlparse
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from neo4j import AsyncGraphDatabase
from neo4j.exceptions import ServiceUnavailable
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from app.core import database
from app.core.processing import ProcessingStage
from app.models.document_upload import Course, DocumentUpload, GraphContinuationJob, GraphExtractionCheckpoint, GraphPublicationIntent, GraphPublicationReceipt
from app.schemas.extraction import ConceptNode, ConceptRelationship, GraphExtractionResponse
from app.services.graph_continuation_service import GraphContinuationService
from app.services.ingestion_service import IngestionService
from app.services.parser_service import DocumentChunk
from app.services.upload_service import UploadService
from scripts.evaluate_graph_continuation_jobs import configuration
from scripts.evaluate_graph_projection import docker, rows
from scripts.evaluate_graph_retry import fixture
from scripts.evaluate_provider_admission import engine_for


async def reconcile_child(args):
    import os
    endpoint = urlparse(args.neo4j_uri)
    if endpoint.scheme != 'bolt' or endpoint.hostname != '127.0.0.1' or not endpoint.port:
        raise ValueError('Only loopback Neo4j fixtures are allowed')
    engine = engine_for(args, args.schema)
    driver = AsyncGraphDatabase.driver(args.neo4j_uri, auth=None)
    class KillCommit(AsyncSession):
        async def commit(self):
            await super().commit()
            os.kill(os.getpid(), signal.SIGKILL)
    try:
        sessions = async_sessionmaker(engine, class_=KillCommit, expire_on_commit=False, autoflush=False)
        with ExitStack() as stack:
            configuration(stack)
            await GraphContinuationService().reconcile_exhausted_publication(sessions, args.intent_id,
                IngestionService(graph_driver=driver, vector_client=SimpleNamespace()))
        raise AssertionError('Reconciliation did not reach commit boundary')
    finally:
        await driver.close()
        await engine.dispose()


async def run(args):
    token = uuid4().hex
    name, schema = 'cg-retry-final-' + token, 'cg_admission_' + token
    admin, engine, driver = engine_for(args), None, None
    container = network = None
    schema_created = False
    service, checks, observations = GraphContinuationService(), {}, {}
    try:
        image_id = docker('image','inspect','neo4j:5-community','--format','{{.Id}}')
        network = docker('network','create','--label',f'conceptgraph.disposable-retry-final={token}',name)
        container = docker('run','--detach','--pull=never','--name',name,
            '--label',f'conceptgraph.disposable-retry-final={token}','--network',network,
            '--publish','127.0.0.1::7687','--memory','1g','--cpus','1','--tmpfs','/data:rw,size=256m',
            '--env','NEO4J_AUTH=none','--env','NEO4J_server_memory_heap_initial__size=256m',
            '--env','NEO4J_server_memory_heap_max__size=256m','--env','NEO4J_server_memory_pagecache_size=64m',
            '--env','NEO4J_dbms_usage__report_enabled=false','--env','NEO4J_db_tx__log_preallocate=false',
            '--env','NEO4J_db_tx__log_rotation_size=16M',image_id)
        binding = docker('port',container,'7687/tcp')
        if not binding.startswith('127.0.0.1:') or '\n' in binding:
            raise RuntimeError('Test Neo4j must be loopback only')
        uri = 'bolt://' + binding
        driver = AsyncGraphDatabase.driver(uri,auth=None,connection_timeout=2)
        deadline = asyncio.get_running_loop().time()+90
        while True:
            try:
                await driver.verify_connectivity()
                break
            except ServiceUnavailable:
                if asyncio.get_running_loop().time()>=deadline: raise
                await asyncio.sleep(1)
        async with admin.begin() as connection:
            version = (await connection.execute(text('SHOW server_version'))).scalar_one()
            await connection.execute(text(f'CREATE SCHEMA {schema}'))
            schema_created = True
        engine = engine_for(args,schema)
        sessions = async_sessionmaker(engine,expire_on_commit=False,autoflush=False)
        with patch.object(database,'postgres_engine',engine): await database.initialize_database_schema()
        ingestion = IngestionService(graph_driver=driver,vector_client=SimpleNamespace())
        class ReaderOnly:
            async def read_graph_publication_marker(self,intent_id):
                return await ingestion.read_graph_publication_marker(intent_id)
        class NoRead:
            async def read_graph_publication_marker(self,intent_id):
                raise AssertionError('Ineligible recovery contacted Neo4j')
        with ExitStack() as stack:
            configuration(stack)
            async with sessions() as session:
                course = Course(id=str(uuid4()),normalized_name='retry final fixture',display_name='Retry final fixture')
                session.add(course)
                await session.commit()

            def graph_for(upload,filename,*,prefix='',connected=True):
                return GraphExtractionResponse(nodes=[ConceptNode(id=key,name=prefix+label,type='concept',
                    upload_id=upload,document_name=filename,source_chunk_id=f'{upload}:{i}',page_number=1,section_heading='DNS')
                    for i,(key,label) in enumerate((('dns','DNS'),('ip','IP address')))],
                    relationships=[ConceptRelationship(source_node_id='dns',target_node_id='ip',relation_type='RELATED_TO')] if connected else [],
                    sections_total=1,sections_succeeded=1,sections_processed=1,batches_total=1,batches_succeeded=1)

            async def prepared(*,connected=True):
                upload,task,_=await fixture(sessions,course,delay=1)
                async with sessions() as session:
                    record=await session.get(DocumentUpload,upload)
                    record.result_json={**record.result_json,'graph_provider_limited':True}
                    await session.commit()
                    chunks=[DocumentChunk(id=f'{upload}:{i}',text=f'DNS evidence {i}.',metadata={
                        'chunk_id':f'{upload}:{i}','document_id':course.id,'upload_id':upload,
                        'execution_token':task,'document_name':record.original_filename,'page_number':1,'section_heading':'DNS'}) for i in range(3)]
                    job=await service.schedule(session,upload_id=upload,document_task_id=task,chunks=chunks,retry_after_seconds=1)
                    job.next_attempt_at=datetime.now(timezone.utc)-timedelta(seconds=1)
                    await session.commit()
                    claim=await service.claim_next(session,lease_owner='final-fixture',lease_seconds=600)
                    assert claim is not None and claim.upload_id==upload
                    graph=graph_for(upload,record.original_filename,connected=connected)
                    candidate=await service.prepare_publication(session,claim,graph)
                    assert await service.save_publication_draft(session,claim,candidate)
                    intent=await service.stage_publication_intent(session,claim)
                    await session.commit()
                return claim,graph,intent

            async def exhaust(claim,*,expired=True,stopped=False,reason='attempt_limit'):
                async with sessions() as session:
                    job=await session.get(GraphContinuationJob,claim.job_id)
                    job.attempt_count=job.max_attempts
                    if stopped:
                        job.status='stopped';job.failure_category=reason
                        job.claim_token=job.lease_owner=job.lease_expires_at=None
                    elif expired:
                        job.lease_expires_at=datetime.now(timezone.utc)-timedelta(seconds=1)
                    await session.commit()

            class Ambiguous(IngestionService):
                async def replace_graph_extraction(self,*items,**kwargs):
                    await super().replace_graph_extraction(*items,**kwargs)
                    raise OSError('synthetic lost acknowledgement')
            async def committed_but_unfinished():
                claim,graph,intent=await prepared()
                try:
                    await service.publish_publication(sessions,claim,Ambiguous(graph_driver=driver,vector_client=SimpleNamespace()))
                except OSError: pass
                else: raise AssertionError('Lost acknowledgement was not reached')
                return claim,graph,intent

            async def state(claim):
                async with sessions() as session:
                    source=await session.get(DocumentUpload,claim.upload_id)
                    job=await session.get(GraphContinuationJob,claim.job_id)
                    receipt=(await session.execute(select(GraphPublicationReceipt).where(GraphPublicationReceipt.job_id==claim.job_id))).scalar_one_or_none()
                    count=(await session.execute(select(func.count()).select_from(GraphExtractionCheckpoint).where(GraphExtractionCheckpoint.upload_id==claim.upload_id))).scalar_one()
                    value={'status':job.status if job else None,'attempts':job.attempt_count if job else None,
                        'max_attempts':job.max_attempts if job else None,'failure':job.failure_category if job else None,
                        'claim':job.claim_token if job else None,'lease_owner':job.lease_owner if job else None,
                        'source_task':source.task_id if source else None,'source_attempt':source.attempt_count if source else None,
                        'source_status':source.status if source else None,'source_stage':source.stage if source else None,
                        'chunks':source.processed_chunk_count if source else None,'graph_status':source.graph_status if source else None,
                        'source_completed_at':source.completed_at.isoformat() if source and source.completed_at else None,
                        'result':source.result_json if source else None,'checkpoints':count,'receipt':bool(receipt)}
                fences=await rows(driver,'MATCH(f:GraphProjectionFence {upload_id:$id}) RETURN f.lock_epoch AS epoch',id=claim.upload_id)
                value['remote_write_epochs']=fences[0]['epoch'] if fences else 0
                return value

            # Adversarial ordering of an already-authorized old remote request.
            claim,old_graph,intent=await prepared(connected=False)
            assert await service.publish_publication(sessions,claim,ingestion)
            old_marker=await ingestion.read_graph_publication_marker(intent.id)
            new_task=str(uuid4())
            async with sessions() as session:
                retried=await UploadService().retry_upload(session,claim.upload_id,new_task)
                assert retried is not None and retried.attempt_count==2
                assert await UploadService().claim_for_processing(session,upload_id=claim.upload_id,
                    task_id=new_task,lease_owner='ordinary-retry',lease_seconds=600)
                await ingestion.cleanup_graph(claim.upload_id,course.id)
                newer=graph_for(claim.upload_id,retried.original_filename,prefix='New ')
                await ingestion.store_graph_extraction(newer,course.id,upload_id=claim.upload_id,
                    document_name=retried.original_filename,execution_token=new_task,course_name=course.display_name)
                assert await UploadService().set_stage(session,claim.upload_id,new_task,ProcessingStage.GRAPH_BUILT,lease_owner='ordinary-retry')
                assert await UploadService().mark_completed(session,claim.upload_id,new_task,
                    {'chunks_indexed':3,'nodes_upserted':2,'relationships_upserted':1,'graph_status':'GRAPH_READY'},lease_owner='ordinary-retry')
            before=await rows(driver,'MATCH(n:Concept {upload_id:$id,execution_token:$task}) RETURN count(n) AS n',id=claim.upload_id,task=new_task)
            rejected=False
            try:
                await ingestion.replace_graph_extraction(old_graph,course.id,upload_id=claim.upload_id,
                    document_name='retry-fixture.pdf',execution_token=claim.document_task_id,course_name=course.display_name,marker=old_marker)
            except ValueError: rejected=True
            after=await rows(driver,'MATCH(n:Concept {upload_id:$id,execution_token:$task}) RETURN count(n) AS n',id=claim.upload_id,task=new_task)
            retry_safe=rejected and after==before
            observations['ordinary_retry_ordering']={'new_task_concepts_before':before[0]['n'],
                'new_task_concepts_after':after[0]['n'],'late_old_write_rejected':rejected,'safe':retry_safe,
                'method':'Explicit replay of the exact old remote operation after real retry and unmarked write; not a timed network-delay experiment.'}
            print(json.dumps(observations['ordinary_retry_ordering']),flush=True)
            assert not await service.publish_publication(sessions,claim,ingestion)
            checks['postgresql_stale_claim_stays_fenced_after_ordinary_retry']=True

            claim,graph,intent=await prepared()
            await exhaust(claim,expired=False)
            child=await asyncio.create_subprocess_exec(sys.executable,str(ROOT/'scripts/evaluate_graph_publication_recovery.py'),
                '--child','--socket-dir',str(args.socket_dir),'--port',str(args.port),'--schema',schema,
                '--neo4j-uri',uri,'--upload-id',claim.upload_id,'--boundary','after_remote_commit',
                stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
            try:
                _,stderr=await asyncio.wait_for(child.communicate(),45)
                assert child.returncode==-signal.SIGKILL,stderr[-500:]
            finally:
                if child.returncode is None: child.kill();await child.wait()
            await exhaust(claim)
            async with sessions() as session:
                assert await service.claim_next(session,lease_owner='must-not-grant-extra-attempt',lease_seconds=60) is None
            before=await state(claim)
            assert before['status']=='stopped' and before['failure']=='attempt_limit' and not before['receipt']
            assert before['attempts']==before['max_attempts'] and before['remote_write_epochs']==1
            checks['last_attempt_crash_preserves_attempt_limit_and_remote_commit']=True
            assert await service.reconcile_exhausted_publication(sessions,intent.id,ReaderOnly())
            done=await state(claim)
            assert done['status']=='completed' and done['receipt'] and done['failure'] is None
            assert done['attempts']==before['attempts']==done['max_attempts'] and done['remote_write_epochs']==1
            for key in ('source_task','source_attempt','source_status','source_stage','chunks','source_completed_at','checkpoints'):
                assert done[key]==before[key],key
            assert done['graph_status']=='GRAPH_READY' and done['claim'] is None and done['lease_owner'] is None
            assert not await service.reconcile_exhausted_publication(sessions,intent.id,NoRead())
            checks['read_only_final_attempt_recovery_finishes_once_without_extra_claim_or_remote_write']=True

            # Ineligible work cannot even read a marker through this path.
            claim,graph,intent=await committed_but_unfinished()
            baseline=await state(claim)
            assert not await service.reconcile_exhausted_publication(sessions,intent.id,NoRead())
            assert await state(claim)==baseline
            await exhaust(claim,expired=False)
            assert not await service.reconcile_exhausted_publication(sessions,intent.id,NoRead())
            await exhaust(claim,stopped=True,reason='superseded')
            assert not await service.reconcile_exhausted_publication(sessions,intent.id,NoRead())
            checks['available_attempts_active_lease_and_other_stop_reasons_do_not_enter_receipt_recovery']=True
            await exhaust(claim,stopped=True)
            async with sessions() as session:
                source=await session.get(DocumentUpload,claim.upload_id)
                source.result_json={**source.result_json,'deletion_requested':True}
                await session.commit()
            baseline=await state(claim)
            assert not await service.reconcile_exhausted_publication(sessions,intent.id,NoRead())
            assert await state(claim)==baseline
            checks['pending_removal_does_not_complete_through_receipt_recovery']=True

            claim,graph,intent=await prepared()
            await exhaust(claim,stopped=True)
            baseline=await state(claim)
            assert not await service.reconcile_exhausted_publication(sessions,intent.id,ReaderOnly())
            assert await state(claim)==baseline
            checks['missing_remote_commit_never_grants_work_or_completes_exhausted_job']=True

            claim,graph,intent=await committed_but_unfinished()
            await exhaust(claim,stopped=True)
            await rows(driver,'MATCH(m:GraphPublication {id:$id}) SET m.payload_sha256=$digest',
                id=intent.id,digest='f'*64)
            baseline=await state(claim)
            try:
                await service.reconcile_exhausted_publication(sessions,intent.id,ReaderOnly())
            except ValueError as exc:
                assert 'marker conflicts' in str(exc)
            else:
                raise AssertionError('Conflicting remote marker was accepted')
            assert await state(claim)==baseline
            checks['conflicting_remote_marker_cannot_complete_or_mutate_bookkeeping']=True

            claim,graph,intent=await committed_but_unfinished()
            await exhaust(claim,stopped=True)
            async with sessions() as session:
                job=await session.get(GraphContinuationJob,claim.job_id)
                job.contract_sha256='f'*64
                await session.commit()
            baseline=await state(claim)
            try:
                await service.reconcile_exhausted_publication(sessions,intent.id,NoRead())
            except ValueError as exc:
                assert 'snapshot/configuration changed' in str(exc)
            else:
                raise AssertionError('Changed extraction contract was accepted')
            assert await state(claim)==baseline
            checks['changed_extraction_contract_fails_before_remote_observation']=True

            claim,graph,intent=await committed_but_unfinished()
            await exhaust(claim) # Running but expired on its last allowed attempt.
            results=await asyncio.gather(*(service.reconcile_exhausted_publication(sessions,intent.id,ReaderOnly()) for _ in range(2)))
            assert sorted(results)==[False,True]
            done=await state(claim)
            assert done['status']=='completed' and done['attempts']==done['max_attempts'] and done['remote_write_epochs']==1
            checks['concurrent_expired_final_attempt_observers_finish_one_receipt_without_new_claim']=True

            claim,graph,intent=await committed_but_unfinished()
            await exhaust(claim,stopped=True)
            baseline=await state(claim)
            class FailedCommit(AsyncSession):
                async def commit(self): raise OSError('synthetic bookkeeping commit failure')
            failing=async_sessionmaker(engine,class_=FailedCommit,expire_on_commit=False,autoflush=False)
            try: await service.reconcile_exhausted_publication(failing,intent.id,ReaderOnly())
            except OSError: pass
            else: raise AssertionError('Commit failure was swallowed')
            assert await state(claim)==baseline
            assert await service.reconcile_exhausted_publication(sessions,intent.id,ReaderOnly())
            checks['bookkeeping_rollback_leaves_retryable_receipt_observation']=True

            claim,graph,intent=await committed_but_unfinished()
            await exhaust(claim,stopped=True)
            observed,release=asyncio.Event(),asyncio.Event()
            class HeldRead(ReaderOnly):
                async def read_graph_publication_marker(self,intent_id):
                    marker=await super().read_graph_publication_marker(intent_id)
                    observed.set()
                    await release.wait()
                    return marker
            recovery=asyncio.create_task(service.reconcile_exhausted_publication(sessions,intent.id,HeldRead()))
            competitor=None
            try:
                await asyncio.wait_for(observed.wait(),10)
                recovery.cancel()
                await asyncio.sleep(0)
                recovery.cancel()
                competitor=asyncio.create_task(service.reconcile_exhausted_publication(sessions,intent.id,ReaderOnly()))
                await asyncio.sleep(0.1)
                assert not recovery.done() and not competitor.done()
                release.set()
                try:
                    await recovery
                except asyncio.CancelledError:
                    pass
                else:
                    raise AssertionError('Caller cancellation was swallowed')
                assert not await asyncio.wait_for(competitor,10)
            finally:
                release.set()
                await asyncio.gather(*(task for task in (recovery,competitor) if task is not None),return_exceptions=True)
            done=await state(claim)
            assert done['status']=='completed' and done['receipt'] and done['attempts']==done['max_attempts'] and done['remote_write_epochs']==1
            checks['repeated_cancellation_drains_receipt_commit_while_source_lock_fences_competitor']=True

            claim,graph,intent=await committed_but_unfinished()
            await exhaust(claim,stopped=True)
            child=await asyncio.create_subprocess_exec(sys.executable,str(Path(__file__).resolve()),
                '--reconcile-child','--socket-dir',str(args.socket_dir),'--port',str(args.port),'--schema',schema,
                '--neo4j-uri',uri,'--intent-id',intent.id,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
            try:
                _,stderr=await asyncio.wait_for(child.communicate(),45)
                assert child.returncode==-signal.SIGKILL,stderr[-500:]
            finally:
                if child.returncode is None:child.kill();await child.wait()
            done=await state(claim)
            assert done['status']=='completed' and done['receipt'] and done['attempts']==done['max_attempts'] and done['remote_write_epochs']==1
            assert not await service.reconcile_exhausted_publication(sessions,intent.id,NoRead())
            checks['sigkill_after_read_only_completion_commit_is_idempotently_durable']=True
    finally:
        errors=[]
        for resource in (driver,engine):
            if resource is not None:
                try:await(resource.close() if resource is driver else resource.dispose())
                except Exception as exc:errors.append(type(exc).__name__)
        if schema_created:
            try:
                async with admin.begin() as connection:await connection.execute(text(f'DROP SCHEMA {schema} CASCADE'))
            except Exception as exc:errors.append(type(exc).__name__)
        try:await admin.dispose()
        except Exception as exc:errors.append(type(exc).__name__)
        for operation in ((('rm','--force','--volumes',container) if container else None),(('network','rm',network) if network else None)):
            if operation:
                try:docker(*operation)
                except Exception as exc:errors.append(type(exc).__name__)
        if errors:raise RuntimeError('Disposable resource cleanup failed: '+', '.join(errors))
    report={'generated_at_utc':datetime.now(timezone.utc).isoformat(),'postgres_version':version,'neo4j_image_id':image_id,
        'checks':checks,'checks_passed':len(checks),'observations':observations,
        'ordinary_retry_race_safe':retry_safe,'activation_blockers':[] if retry_safe else ['An old marked replay can overwrite a newer ordinary unmarked retry projection.'],
        'runtime_dispatch_connected':False,'phase3_complete':False,'temporary_resources_removed':True,
        'hosted_provider_calls':0,'production_test_data_mutations':0,
        'source_sha256':{name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in (
            'app/services/graph_continuation_service.py','app/services/ingestion_service.py','app/services/upload_service.py',
            'app/services/graph_result_metadata.py','scripts/evaluate_graph_retry_final_attempt.py',
            'scripts/evaluate_graph_publication_recovery.py','tests/test_graph_final_attempt_recovery.py')},
        'limits':['Ordinary retry uses real upload lifecycle methods and unmarked graph methods, but not the live API, vector writes or full processor.',
            'Old remote work is adversarially replayed after the retry; this does not emulate packet delay or every server timing.',
            'The final attempt is reached by advancing the private fixture counter, not consuming provider attempts; its lease is explicitly expired.',
            'Recovery only observes exact existing markers; it must not extract, publish or grant another claim.',
            'No models, Qdrant, R2, hosted data, production test mutations or automatic dispatcher are exercised.']}
    output=ROOT/'evaluation/graph-retry-final-attempt-current'
    output.with_suffix('.json').write_text(json.dumps(report,indent=2)+'\n')
    output.with_suffix('.md').write_text('# Retry ordering and final-attempt recovery — isolated stores\n\n'
        'Command: `.venv/bin/python scripts/evaluate_graph_retry_final_attempt.py --socket-dir <private-test-socket>`.\n\n'
        +f"{len(checks)} recovery/invariant checks passed. Ordinary retry ordering safe: **{retry_safe}**. Automatic dispatch remains disabled.\n\n"
        +'\n'.join(f'- {name}: passed' for name in checks)+'\n\nActivation blockers:\n\n'
        +'\n'.join(f'- {item}' for item in report['activation_blockers'])+'\n\nLimits:\n\n'
        +'\n'.join(f'- {item}' for item in report['limits'])+'\n')
    print(json.dumps({'checks_passed':len(checks),'ordinary_retry_race_safe':retry_safe,'temporary_resources_removed':True}))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--socket-dir',type=Path,required=True)
    parser.add_argument('--port',type=int,default=55432)
    parser.add_argument('--reconcile-child',action='store_true')
    parser.add_argument('--schema',default='')
    parser.add_argument('--neo4j-uri',default='')
    parser.add_argument('--intent-id',default='')
    args=parser.parse_args()
    asyncio.run(reconcile_child(args) if args.reconcile_child else run(args))
