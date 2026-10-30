"""Real local PostgreSQL/Neo4j publication, recovery and optional gated idle dispatch.

Uses synthetic graphs, a private PostgreSQL socket, and an owned disposable
Neo4j container. No model, embedding, Qdrant, R2 or production request.
"""

import argparse
import asyncio
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import signal
import sys
import time
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
from app.models.document_upload import Course, DocumentUpload, GraphContinuationJob, GraphExtractionCheckpoint, GraphPublicationIntent, GraphPublicationReceipt
from app.schemas.extraction import ConceptNode, ConceptRelationship, GraphExtractionResponse
from app.services.graph_continuation_service import GraphContinuationService, GraphJobClaim
from app.services.ingestion_service import IngestionService
from app.services.parser_service import DocumentChunk
from app.services.upload_service import UploadService
from scripts.evaluate_graph_continuation_jobs import configuration
from scripts.evaluate_graph_projection import docker, rows
from scripts.evaluate_graph_retry import fixture
from scripts.evaluate_provider_admission import engine_for


async def child(args):
    endpoint = urlparse(args.neo4j_uri)
    if endpoint.scheme != 'bolt' or endpoint.hostname != '127.0.0.1' or not endpoint.port:
        raise ValueError('Child accepts a loopback-only Neo4j fixture.')
    engine = engine_for(args, args.schema)
    driver = AsyncGraphDatabase.driver(args.neo4j_uri, auth=None)
    commits = 0

    def kill():
        os.kill(os.getpid(), signal.SIGKILL)

    class CrashSession(AsyncSession):
        async def commit(self):
            nonlocal commits
            commits += 1
            if args.boundary == 'before_intent_commit' and commits == 1:
                kill()
            await super().commit()
            if (args.boundary == 'after_intent_commit' and commits == 1
                    or args.boundary == 'after_result_commit' and commits == 2):
                kill()

    class CrashIngestion(IngestionService):
        async def _write_graph_extraction(self, *items, **kwargs):
            await super()._write_graph_extraction(*items, **kwargs)
            if args.boundary == 'during_remote_transaction':
                kill()

        async def replace_graph_extraction(self, *items, **kwargs):
            await super().replace_graph_extraction(*items, **kwargs)
            if args.boundary == 'after_remote_commit':
                kill()

    try:
        sessions = async_sessionmaker(engine, class_=CrashSession, expire_on_commit=False, autoflush=False)
        async with sessions() as session:
            job = (await session.execute(select(GraphContinuationJob).where(
                GraphContinuationJob.upload_id == args.upload_id))).scalar_one()
            claim = GraphJobClaim(job.id, args.upload_id, job.document_task_id,
                job.claim_token, job.lease_owner, ())
        with ExitStack() as stack:
            configuration(stack)
            await GraphContinuationService().publish_publication(sessions, claim,
                CrashIngestion(graph_driver=driver, vector_client=SimpleNamespace()))
        raise AssertionError('Crash boundary was not reached')
    finally:
        await driver.close()
        await engine.dispose()


async def run(args):
    token = uuid4().hex
    name, schema = 'cg-publication-test-' + token, 'cg_admission_' + token
    admin, engine, driver = engine_for(args), None, None
    container = network = None
    schema_created = False
    service, checks, observations = GraphContinuationService(), {}, {}
    started = time.perf_counter()
    try:
        image_id = docker('image', 'inspect', 'neo4j:5-community', '--format', '{{.Id}}')
        network = docker('network', 'create', '--label', f'conceptgraph.disposable-publication={token}', name)
        container = docker('run', '--detach', '--pull=never', '--name', name,
            '--label', f'conceptgraph.disposable-publication={token}', '--network', network,
            '--publish', '127.0.0.1::7687', '--memory', '1g', '--cpus', '1',
            '--tmpfs', '/data:rw,size=256m', '--env', 'NEO4J_AUTH=none',
            '--env', 'NEO4J_server_memory_heap_initial__size=256m',
            '--env', 'NEO4J_server_memory_heap_max__size=256m',
            '--env', 'NEO4J_server_memory_pagecache_size=64m',
            '--env', 'NEO4J_dbms_usage__report_enabled=false',
            '--env', 'NEO4J_db_tx__log_preallocate=false',
            '--env', 'NEO4J_db_tx__log_rotation_size=16M', image_id)
        binding = docker('port', container, '7687/tcp')
        if not binding.startswith('127.0.0.1:') or '\n' in binding:
            raise RuntimeError('Neo4j must be loopback only')
        uri = 'bolt://' + binding
        driver = AsyncGraphDatabase.driver(uri, auth=None, connection_timeout=2)
        deadline = time.monotonic() + 90
        while True:
            try:
                await driver.verify_connectivity()
                break
            except ServiceUnavailable:
                if time.monotonic() >= deadline:
                    raise
                await asyncio.sleep(1)
        async with admin.begin() as connection:
            version = (await connection.execute(text('SHOW server_version'))).scalar_one()
            await connection.execute(text(f'CREATE SCHEMA {schema}'))
            schema_created = True
        engine = engine_for(args, schema)
        sessions = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        with patch.object(database, 'postgres_engine', engine):
            await database.initialize_database_schema()
        ingestion = IngestionService(graph_driver=driver, vector_client=SimpleNamespace())
        with ExitStack() as stack:
            configuration(stack)
            async with sessions() as session:
                course = Course(id=str(uuid4()), normalized_name='publication fixture', display_name='Publication fixture')
                session.add(course)
                await session.commit()

            async def prepared(*, limited=False, save_draft=True):
                upload_id, task, _ = await fixture(sessions, course, delay=1)
                async with sessions() as session:
                    record = await session.get(DocumentUpload, upload_id)
                    record.result_json = {**record.result_json, 'graph_provider_limited': True}
                    await session.commit()
                    chunks = [DocumentChunk(id=f'{upload_id}:{i}', text=f'DNS evidence {i}.', metadata={
                        'chunk_id': f'{upload_id}:{i}', 'document_id': course.id, 'upload_id': upload_id,
                        'execution_token': task, 'document_name': record.original_filename,
                        'page_number': 1, 'section_heading': 'DNS'}) for i in range(3)]
                    job = await service.schedule(session, upload_id=upload_id, document_task_id=task,
                        chunks=chunks, retry_after_seconds=1)
                    job.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
                    await session.commit()
                    claim = await service.claim_next(session, lease_owner='fixture-publisher', lease_seconds=600)
                    assert claim is not None and claim.upload_id == upload_id
                    nodes = [ConceptNode(id=key, name=label, type='concept', upload_id=upload_id,
                        document_name=record.original_filename, source_chunk_id=chunks[i].id,
                        page_number=1, section_heading='DNS')
                        for i, (key, label) in enumerate((('dns', 'DNS'), ('ip', 'IP address')))]
                    graph = GraphExtractionResponse(nodes=nodes, relationships=[ConceptRelationship(
                        source_node_id='dns', target_node_id='ip', relation_type='RELATED_TO')],
                        sections_total=1, sections_succeeded=1, sections_processed=1,
                        batches_total=1, batches_succeeded=1, provider_limited=limited)
                    if save_draft:
                        candidate = await service.prepare_publication(session, claim, graph)
                        assert await service.save_publication_draft(session, claim, candidate)
                        await session.commit()
                    old = GraphExtractionResponse(nodes=[ConceptNode(id='old-' + node.id, name='Old ' + node.name,
                        type='concept', upload_id=upload_id, document_name=record.original_filename,
                        source_chunk_id=node.source_chunk_id, page_number=1, section_heading='DNS') for node in nodes])
                    await ingestion.store_graph_extraction(old, course.id, upload_id=upload_id,
                        document_name=record.original_filename, course_name=course.display_name, execution_token=task)
                return claim

            async def inspect(claim):
                async with sessions() as session:
                    record = await session.get(DocumentUpload, claim.upload_id)
                    job = await session.get(GraphContinuationJob, claim.job_id)
                    intents = (await session.execute(select(GraphPublicationIntent).where(
                        GraphPublicationIntent.job_id == claim.job_id))).scalars().all()
                    receipts = (await session.execute(select(func.count()).select_from(GraphPublicationReceipt).where(
                        GraphPublicationReceipt.job_id == claim.job_id))).scalar_one()
                    checkpoints = (await session.execute(select(func.count()).select_from(GraphExtractionCheckpoint).where(
                        GraphExtractionCheckpoint.upload_id == claim.upload_id))).scalar_one()
                    result = {'job': job.status if job else None, 'intents': len(intents), 'receipts': receipts,
                        'source_status': record.status if record else None,
                        'source_task_unchanged': bool(record and record.task_id == claim.document_task_id),
                        'chunks': record.processed_chunk_count if record else None,
                        'graph_status': record.graph_status if record else None, 'checkpoints': checkpoints}
                result['names'] = [row['name'] for row in await rows(driver,
                    'MATCH (n:Concept {upload_id:$id}) RETURN n.name AS name ORDER BY name', id=claim.upload_id)]
                result['markers'] = (await rows(driver, 'MATCH (m:GraphPublication {upload_id:$id}) RETURN count(m) AS n',
                    id=claim.upload_id))[0]['n']
                fences = await rows(driver, 'MATCH (f:GraphProjectionFence {upload_id:$id}) RETURN f.lock_epoch AS epoch', id=claim.upload_id)
                result['write_epochs'] = fences[0]['epoch'] if fences else 0
                return result

            def completed(value):
                assert value == {'job':'completed', 'intents':1, 'receipts':1, 'source_status':'ready',
                    'source_task_unchanged':True, 'chunks':3, 'graph_status':'GRAPH_READY', 'checkpoints':1,
                    'names':['DNS','IP address'], 'markers':1, 'write_epochs':1}, value

            claim = await prepared()
            assert await service.publish_publication(sessions, claim, ingestion)
            completed(await inspect(claim))
            checks['real_marked_publication_and_postgresql_completion'] = True

            if args.graph_only_processor or args.idle_graph_dispatch:
                from app.core.config import Settings
                from app.services.graph_only_processor import GraphOnlyProcessor
                from app.services.graph_job_extraction import GraphJobDeferred
                from app.models.document_upload import GraphPublicationDraft
                cfg = Settings(_env_file=None, GRAPH_PROJECTION_FENCING_ENABLED=True,
                    GRAPH_CONTINUATION_SNAPSHOTS_ENABLED=True, PROCESSING_ROLE='worker')

                class AuthoredGraph(IngestionService):
                    calls = 0
                    limited = False
                    async def extract_graph_from_chunks(self, chunks, **kwargs):
                        self.calls += 1
                        assert isinstance(kwargs['completed_batches'], dict)
                        nodes = [ConceptNode(id=key, name=label, type='concept',
                            upload_id=chunks[0].metadata['upload_id'],
                            document_name=chunks[0].metadata['document_name'],
                            source_chunk_id=chunks[i].id, page_number=1, section_heading='DNS')
                            for i,(key,label) in enumerate((('dns','DNS'),('ip','IP address')))]
                        return GraphExtractionResponse(nodes=nodes, relationships=[ConceptRelationship(
                            source_node_id='dns',target_node_id='ip',relation_type='RELATED_TO')],
                            sections_total=1, sections_succeeded=1, sections_processed=1,
                            batches_total=1, batches_succeeded=1, provider_limited=self.limited,
                            provider_retry_after_seconds=30 if self.limited else None)

                authored = AuthoredGraph(graph_driver=driver, vector_client=SimpleNamespace())
                processor = GraphOnlyProcessor(sessions, authored, config=cfg, jobs=service)
                claim = await prepared()
                assert await processor.process(claim)
                assert authored.calls == 0
                completed(await inspect(claim))
                checks['processor_saved_draft_replay_skips_extraction_and_preserves_ready_identity'] = True

                claim = await prepared(save_draft=False)
                assert await processor.process(claim)
                assert authored.calls == 1
                completed(await inspect(claim))
                checks['processor_fresh_authoritative_snapshot_stages_and_publishes_under_real_locks'] = True

                claim = await prepared(save_draft=False)
                before = await inspect(claim)
                authored.limited = True
                try:
                    await processor.process(claim)
                    raise AssertionError('Quota-limited work must not complete or publish')
                except GraphJobDeferred:
                    pass
                after = await inspect(claim)
                assert after['job'] == 'pending' and after['source_status'] == 'ready'
                assert after['source_task_unchanged'] and after['names'] == before['names']
                assert after['markers'] == after['intents'] == after['receipts'] == 0
                assert authored.calls == 2
                async with sessions() as session:
                    job = await session.get(GraphContinuationJob, claim.job_id)
                    now = (await session.execute(select(func.clock_timestamp()))).scalar_one()
                    assert job.next_attempt_at > now and job.lease_owner is None and job.claim_token is None
                    assert await session.get(GraphPublicationDraft, claim.job_id) is None
                observations['graph_only_processor'] = {'saved_draft_extraction_calls':0,
                    'fresh_authored_extraction_calls':1, 'quota_authored_extraction_calls':1,
                    'ready_document_task_preserved':True, 'quota_deadline_persisted':True,
                    'automatic_dispatch_exercised':False, 'actual_provider_calls':0,
                    'parser_embedding_vector_and_source_interfaces_not_supplied':True}
                checks['processor_quota_defers_durably_without_draft_or_projection_changes'] = True

            if args.idle_graph_dispatch:
                from scripts.idle_graph_dispatch_checks import qualify_idle_dispatch
                await qualify_idle_dispatch(sessions, service, authored, prepared, inspect, completed, checks, observations)

            boundaries = ('before_intent_commit', 'after_intent_commit', 'during_remote_transaction',
                'after_remote_commit', 'after_result_commit')
            for boundary in boundaries:
                claim = await prepared()
                proc = await asyncio.create_subprocess_exec(sys.executable, str(Path(__file__).resolve()),
                    '--child', '--socket-dir', str(args.socket_dir), '--port', str(args.port), '--schema', schema,
                    '--neo4j-uri', uri, '--upload-id', claim.upload_id, '--boundary', boundary,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                try:
                    _, stderr = await asyncio.wait_for(proc.communicate(), 45)
                    assert proc.returncode == -signal.SIGKILL, f'{boundary}: {stderr[-700:]}'
                finally:
                    if proc.returncode is None:
                        proc.kill()
                        await proc.wait()
                value = await inspect(claim)
                observations[boundary] = value
                if boundary == 'after_result_commit':
                    completed(value)
                    assert not await service.publish_publication(sessions, claim, ingestion)
                else:
                    assert value['job'] == 'running' and value['receipts'] == 0 and value['graph_status'] == 'GRAPH_PARTIAL', value
                    assert value['intents'] == (0 if boundary == 'before_intent_commit' else 1), value
                    assert value['names'] == (['DNS','IP address'] if boundary == 'after_remote_commit' else ['Old DNS','Old IP address']), value
                    assert value['markers'] == (1 if boundary == 'after_remote_commit' else 0), value
                    async with sessions() as session:
                        job = await session.get(GraphContinuationJob, claim.job_id)
                        job.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
                        await session.commit()
                        recovered = await service.claim_next(session, lease_owner='fresh-recovery', lease_seconds=600)
                    assert recovered is not None and recovered.upload_id == claim.upload_id
                    assert recovered.claim_token != claim.claim_token
                    assert not await service.publish_publication(sessions, claim, ingestion)
                    assert await service.publish_publication(sessions, recovered, ingestion)
                completed(await inspect(claim))
                checks[f'sigkill_{boundary}_recovers_without_duplicate_remote_write'] = True
                print(json.dumps({'boundary_verified':boundary}), flush=True)

            class Ambiguous(IngestionService):
                async def replace_graph_extraction(self, *items, **kwargs):
                    await super().replace_graph_extraction(*items, **kwargs)
                    raise OSError('synthetic lost acknowledgement after remote commit')
            claim = await prepared()
            try:
                await service.publish_publication(sessions, claim, Ambiguous(graph_driver=driver, vector_client=SimpleNamespace()))
            except OSError:
                pass
            else:
                raise AssertionError('Ambiguous remote commit did not propagate')
            assert (await inspect(claim))['job'] == 'running'
            assert await service.publish_publication(sessions, claim, ingestion)
            completed(await inspect(claim))
            checks['lost_remote_acknowledgement_reuses_exact_marker'] = True

            reached, release = asyncio.Event(), asyncio.Event()
            class SlowAcknowledgement(IngestionService):
                async def replace_graph_extraction(self, *items, **kwargs):
                    await super().replace_graph_extraction(*items, **kwargs)
                    reached.set()
                    await release.wait()
            claim = await prepared()
            task = asyncio.create_task(service.publish_publication(sessions, claim,
                SlowAcknowledgement(graph_driver=driver, vector_client=SimpleNamespace())))
            try:
                await asyncio.wait_for(reached.wait(), 20)
                task.cancel()
                await asyncio.sleep(.02)
                task.cancel()
                async with sessions() as session:
                    assert (await session.execute(select(DocumentUpload.upload_id).where(
                        DocumentUpload.upload_id == claim.upload_id).with_for_update(skip_locked=True))).scalar_one_or_none() is None
                release.set()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                else:
                    raise AssertionError('Caller cancellation was swallowed')
                completed(await inspect(claim))
            finally:
                release.set()
                if not task.done():
                    task.cancel()
                    try:
                        await asyncio.wait_for(task, 20)
                    except asyncio.CancelledError:
                        pass
            checks['repeated_cancellation_drains_full_unit_before_releasing_source_lock'] = True

            claim = await prepared()
            results = await asyncio.gather(*(service.publish_publication(sessions, claim, ingestion) for _ in range(2)))
            assert sorted(results) == [False, True]
            completed(await inspect(claim))
            checks['competing_publishers_commit_one_projection_and_one_receipt'] = True

            claim = await prepared()
            entered, resume = asyncio.Event(), asyncio.Event()
            class GapSession(AsyncSession):
                async def commit(self):
                    await super().commit()
                    entered.set()
                    await resume.wait()
            gap_sessions = async_sessionmaker(engine, class_=GapSession, expire_on_commit=False, autoflush=False)
            task = asyncio.create_task(service.publish_publication(gap_sessions, claim, ingestion))
            try:
                await asyncio.wait_for(entered.wait(), 20)
                async with sessions() as session:
                    await UploadService().lock_for_deletion(session, claim.upload_id)
                    await ingestion.cleanup_graph(claim.upload_id, course.id)
                    await UploadService().delete_document(session, claim.upload_id)
                resume.set()
                assert not await asyncio.wait_for(task, 20)
                value = await inspect(claim)
                assert value['source_status'] is None and value['job'] is None and value['names'] == [] and value['markers'] == 0
            finally:
                resume.set()
                if not task.done():
                    await asyncio.wait_for(task, 20)
            checks['committed_source_removal_in_intent_gap_blocks_remote_publication'] = True

            claim = await prepared()
            async with sessions() as session:
                intent = await service.stage_publication_intent(session, claim)
                await session.commit()
                await rows(driver, 'CREATE (m:GraphPublication {id:$id, job_id:"wrong-job", upload_id:$upload, '
                    'course_id:$course, document_task_id:$task, payload_sha256:$sha, generation:1})',
                    id=intent.id, upload=claim.upload_id, course=course.id, task=claim.document_task_id, sha='b'*64)
            try:
                await service.publish_publication(sessions, claim, ingestion)
            except ValueError:
                pass
            else:
                raise AssertionError('Conflicting marker authorized a write')
            value = await inspect(claim)
            assert value['job'] == 'running' and value['receipts'] == 0 and value['names'] == ['Old DNS','Old IP address']
            checks['conflicting_remote_marker_fails_closed_without_replacement'] = True

            claim = await prepared(limited=True)
            try:
                await service.publish_publication(sessions, claim, ingestion)
            except ValueError:
                pass
            else:
                raise AssertionError('Provider-limited draft was published')
            value = await inspect(claim)
            assert value['job'] == 'running' and value['markers'] == 0 and value['names'] == ['Old DNS','Old IP address']
            checks['provider_limited_candidate_never_writes_remote_projection'] = True
    finally:
        errors = []
        for resource in (driver, engine):
            if resource is not None:
                try:
                    await (resource.close() if resource is driver else resource.dispose())
                except Exception as exc:
                    errors.append(type(exc).__name__)
        if schema_created:
            try:
                async with admin.begin() as connection:
                    await connection.execute(text(f'DROP SCHEMA {schema} CASCADE'))
            except Exception as exc:
                errors.append(type(exc).__name__)
        try:
            await admin.dispose()
        except Exception as exc:
            errors.append(type(exc).__name__)
        for operation in ((('rm', '--force', '--volumes', container) if container else None),
                          (('network', 'rm', network) if network else None)):
            if operation:
                try:
                    docker(*operation)
                except Exception as exc:
                    errors.append(type(exc).__name__)
        if errors:
            raise RuntimeError('Disposable resource cleanup failed: ' + ', '.join(errors))
    report = {'generated_at_utc':datetime.now(timezone.utc).isoformat(), 'postgres_version':version,
        'neo4j_image_id':image_id, 'checks':checks, 'checks_passed':len(checks), 'crash_observations':observations,
        'hosted_provider_calls':0, 'production_test_data_mutations':0, 'temporary_resources_removed':True,
        'elapsed_seconds':round(time.perf_counter()-started, 3),
        'source_sha256':{name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in (
            'app/services/graph_continuation_service.py','app/services/ingestion_service.py',
            'app/core/blocking.py','app/models/document_upload.py','app/services/upload_service.py',
            'app/services/graph_result_metadata.py','app/services/graph_publication_candidate.py',
            'tests/test_graph_publication_coordinator.py','scripts/evaluate_graph_publication_recovery.py',
            'app/services/graph_only_processor.py','app/services/graph_job_extraction.py',
            'app/services/resource_budget_service.py','app/core/config.py',
            'tests/test_graph_only_processor.py','scripts/evaluate_graph_continuation_jobs.py',
            'app/core/processing_coordinator.py','scripts/idle_graph_dispatch_checks.py',
            'tests/test_idle_graph_dispatch.py')},
        'limits':['Synthetic source/checkpoint/graph fixtures; no model, embedding, Qdrant, R2 or live API ingestion.',
            'Crash recovery explicitly expires the isolated fixture lease before a fresh claim; no natural lease-duration or full worker benchmark.',
            'Source removal in the intent gap uses PostgreSQL and Neo4j only; full API/Qdrant/R2 deletion is covered separately.',
            'Hard kills cover five instrumented boundaries, not every network timing or delayed server commit.',
            'Existing unmarked ingestion/retry writes, superseded source generations, exhausted recovery, retention and combined dispatch still require verification before activation.',
            'Production dispatch defaults off. Optional idle-coordinator checks share one event loop, not separate OS workers or a hard-kill worker benchmark. Test elapsed time is not throughput or capacity.']}
    output = ROOT/('evaluation/idle-graph-dispatch-current' if args.idle_graph_dispatch
                   else 'evaluation/graph-only-processor-current' if args.graph_only_processor
                   else 'evaluation/graph-publication-recovery-current')
    output.with_suffix('.json').write_text(json.dumps(report, indent=2)+'\n')
    output.with_suffix('.md').write_text(('# Idle graph dispatch — local PostgreSQL and Neo4j\n\n' if args.idle_graph_dispatch
        else '# Graph-only processor — local PostgreSQL and Neo4j\n\n'
        if args.graph_only_processor else '# Graph publication recovery — local PostgreSQL and Neo4j\n\n') +
        'Command: `.venv/bin/python scripts/evaluate_graph_publication_recovery.py --socket-dir <private-test-socket>'
        + (' --idle-graph-dispatch' if args.idle_graph_dispatch else ' --graph-only-processor' if args.graph_only_processor else '') + '`.\n\n'
        + '\n'.join(f'- {name}: passed' for name in checks)+'\n\nLimits:\n\n'
        + '\n'.join(f'- {item}' for item in report['limits'])+'\n')
    print(json.dumps({'checks_passed':len(checks),'hosted_provider_calls':0,'temporary_resources_removed':True}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--socket-dir', type=Path, required=True)
    parser.add_argument('--port', type=int, default=55432)
    parser.add_argument('--child', action='store_true')
    parser.add_argument('--graph-only-processor', action='store_true')
    parser.add_argument('--idle-graph-dispatch', action='store_true')
    parser.add_argument('--schema', default='')
    parser.add_argument('--neo4j-uri', default='')
    parser.add_argument('--upload-id', default='')
    parser.add_argument('--boundary', choices=('before_intent_commit','after_intent_commit','during_remote_transaction','after_remote_commit','after_result_commit'))
    args = parser.parse_args()
    asyncio.run(child(args) if args.child else run(args))
