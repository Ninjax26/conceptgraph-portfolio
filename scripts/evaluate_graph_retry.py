"""Verify durable retry eligibility with isolated PostgreSQL and the real API.

No provider, vector, graph or source-storage requests. Commit counts are authored
fixtures: this tests persistence/fencing, not cross-store consistency or ingestion.
"""

import argparse
import asyncio
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx
from fastapi import FastAPI
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.endpoints import ingest
from app.core import database
from app.core.config import Settings
from app.core.processing import ProcessingStage
from app.core.processing_coordinator import EnqueueDisposition
from app.models.document_upload import Course, DocumentUpload, GraphExtractionCheckpoint, ProcessingAttempt
from app.services.course_service import CourseService
from app.services.graph_retry_policy import GraphRetryNotDue, retry_wait_seconds
from app.services.resource_budget_service import resource_budget_service
from app.services.upload_service import UploadService
from scripts.evaluate_provider_admission import engine_for


async def fixture(sessions, course, *, delay=120):
    upload_id, task_id = str(uuid4()), str(uuid4())
    service = UploadService()
    async with sessions() as session:
        await service.create_upload(session, upload_id=upload_id, task_id=task_id, course=course,
            content_hash="a"*64, original_filename="retry-fixture.pdf", storage_key=f"fixture/{upload_id}",
            page_count=1, source_bytes=100, reserved_chunk_count=3)
        await service.claim_for_processing(session, upload_id=upload_id, task_id=task_id,
                                           lease_owner="fixture-worker", lease_seconds=180)
        assert await service.set_stage(session, upload_id, task_id, ProcessingStage.GRAPH_BUILT,
                                       lease_owner="fixture-worker")
        checkpoint = {"nodes":[], "relationships":[], "empty_extraction":True}
        assert await service.save_graph_checkpoint(session, upload_id=upload_id, task_id=task_id,
            lease_owner="fixture-worker", batch_key="fixture-completed-batch", section_key="fixture",
            section_label="Completed fixture section", extraction_json=checkpoint)
        result = {"chunks_indexed":3, "nodes_upserted":2, "relationships_upserted":0,
                  "graph_status":"GRAPH_PARTIAL", "graph_batches_failed":1, "graph_batches_skipped":1}
        if delay is not None:
            result["graph_retry_after_seconds"] = delay
        assert await service.mark_completed(session, upload_id, task_id, result, lease_owner="fixture-worker")
    return upload_id, task_id, checkpoint


async def child_check(args):
    engine = engine_for(args, args.schema)
    sessions = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        async with sessions() as session:
            try:
                await UploadService().retry_upload(session, args.upload_id, str(uuid4()))
            except GraphRetryNotDue:
                row = await UploadService().get_upload(session, args.upload_id)
                assert row.stage == "READY" and row.attempt_count == 1
                print(json.dumps({"pid":os.getpid(), "not_due":True, "attempt_count":row.attempt_count}))
            else:
                raise AssertionError("A new process bypassed the persisted deadline")
    finally:
        await engine.dispose()


async def run(args):
    admin = engine_for(args)
    schema = "cg_admission_"+uuid4().hex
    engine, created = None, False
    checks = {}
    try:
        async with admin.begin() as connection:
            postgres_version = (await connection.execute(text("SHOW server_version"))).scalar_one()
            await connection.execute(text(f"CREATE SCHEMA {schema}"))
            created = True
        engine = engine_for(args, schema)
        sessions = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        with patch.object(database, "postgres_engine", engine):
            await database.initialize_database_schema()
        course = Course(id=str(uuid4()), normalized_name="graph retry fixture", display_name="Graph retry fixture")
        async with sessions() as session:
            session.add(course)
            await session.commit()
        upload_id, old_task, checkpoint = await fixture(sessions, course)
        service = UploadService()
        async with sessions() as session:
            row = await service.get_upload(session, upload_id)
            server_now = (await session.execute(select(func.clock_timestamp()))).scalar_one()
            assert 1 <= retry_wait_seconds(row.result_json, server_now) <= 120
            original_result = dict(row.result_json)
        checks["deadline_persisted_from_database_clock"] = True

        process = await asyncio.create_subprocess_exec(sys.executable, str(Path(__file__).resolve()), "--child-check",
            "--socket-dir", str(args.socket_dir), "--port", str(args.port), "--schema", schema, "--upload-id", upload_id,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
            assert process.returncode == 0, "Isolated subprocess retry check failed"
            child_result = json.loads(stdout)
            assert child_result["pid"] != os.getpid() and child_result["not_due"]
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        checks["fresh_process_cannot_bypass_persisted_deadline"] = True

        async def dependency():
            async with sessions() as session:
                yield session
        app = FastAPI()
        app.include_router(ingest.router)
        app.dependency_overrides[database.get_postgres_session] = dependency
        with ExitStack() as stack:
            stack.enter_context(patch.object(resource_budget_service, "config", Settings(_env_file=None, RERANK_PROVIDER="local")))
            exists = stack.enter_context(patch.object(ingest.storage_service, "exists", Mock(return_value=True)))
            enqueue = stack.enter_context(patch.object(ingest.processing_coordinator, "submit", AsyncMock(return_value=EnqueueDisposition.DEFERRED)))
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
                early = await asyncio.gather(*(client.post(f"/api/v1/ingest/uploads/{upload_id}/retry") for _ in range(3)))
                assert all(response.status_code == 429 and int(response.headers["Retry-After"]) > 0 for response in early)
                exists.assert_not_called()
                enqueue.assert_not_awaited()
                async with sessions() as session:
                    row = await service.get_upload(session, upload_id)
                    assert row.task_id == old_task and row.attempt_count == 1 and row.stage == "READY"
                    assert row.result_json == original_result
                    assert (await service.load_graph_checkpoints(session, upload_id))[0].extraction_json == checkpoint
                    context = await CourseService().get_ready_context(session, course.id)
                    assert upload_id in context.document_ids
                    count = (await session.execute(select(func.count()).select_from(ProcessingAttempt))).scalar_one()
                    assert count == 1
                checks["premature_concurrent_api_retries_preserve_ready_state_attempts_and_checkpoint"] = True
                checks["ready_document_remains_query_eligible_during_wait"] = True

                async with sessions() as session:
                    row = await service.get_upload(session, upload_id)
                    now = (await session.execute(select(func.clock_timestamp()))).scalar_one()
                    row.result_json = {**row.result_json, "graph_retry_not_before":(now-timedelta(seconds=1)).isoformat()}
                    await session.commit()
                due = await asyncio.gather(*(client.post(f"/api/v1/ingest/uploads/{upload_id}/retry") for _ in range(2)))
                assert sorted(response.status_code for response in due) == [200,409]
                exists.assert_called_once()
                enqueue.assert_awaited_once()
                async with sessions() as session:
                    row = await service.get_upload(session, upload_id)
                    assert row.attempt_count == 2 and row.stage == "UPLOADED" and row.task_id != old_task
                    assert row.result_json is None
                    assert (await service.load_graph_checkpoints(session, upload_id))[0].extraction_json == checkpoint
                    assert (await session.execute(select(func.count()).select_from(ProcessingAttempt))).scalar_one() == 2
                    assert not await service.save_graph_checkpoint(session, upload_id=upload_id, task_id=old_task,
                        lease_owner="fixture-worker", batch_key="stale", section_key="stale", section_label="stale", extraction_json=checkpoint)
                checks["expired_deadline_allows_exactly_one_concurrent_attempt"] = True
                checks["checkpoint_survives_retry_and_stale_task_is_fenced"] = True

        async with sessions() as stale_session:
            cached = await service.get_upload(stale_session, upload_id)
            current_task = cached.task_id
            async with sessions() as winner:
                assert await service.claim_for_processing(winner, upload_id=upload_id, task_id=current_task,
                    lease_owner="claim-winner", lease_seconds=180) is not None
            assert await service.claim_for_processing(stale_session, upload_id=upload_id, task_id=current_task,
                lease_owner="claim-loser", lease_seconds=180) is None
        async with sessions() as session:
            row = await service.get_upload(session, upload_id)
            assert row.lease_owner == "claim-winner"
        checks["cached_unclaimed_read_cannot_steal_current_worker_lease"] = True

        waiting_id, _, _ = await fixture(sessions, course)
        async with sessions() as session:
            assert await service.lock_for_deletion(session, waiting_id)
            assert await service.delete_document(session, waiting_id)
            assert not await service.load_graph_checkpoints(session, waiting_id)
            assert await service.retry_upload(session, waiting_id, str(uuid4())) is None
        checks["waiting_ready_document_deletion_cascades_checkpoint"] = True

        legacy_id, _, _ = await fixture(sessions, course, delay=None)
        async with sessions() as session:
            assert await service.retry_upload(session, legacy_id, str(uuid4())) is not None
        checks["legacy_record_without_deadline_keeps_existing_retry_behavior"] = True

        deletion_race_id, _, _ = await fixture(sessions, course, delay=None)
        async with sessions() as stale_session:
            cached = await service.get_upload(stale_session, deletion_race_id)
            assert cached.stage == "READY"
            async with sessions() as winner:
                assert await service.retry_upload(winner, deletion_race_id, str(uuid4())) is not None
            assert await service.lock_for_deletion(stale_session, deletion_race_id) is None
        async with sessions() as session:
            row = await service.get_upload(session, deletion_race_id)
            assert row.stage == "UPLOADED" and row.attempt_count == 2
            assert await service.load_graph_checkpoints(session, deletion_race_id)
        checks["cached_ready_deletion_read_cannot_delete_new_active_attempt"] = True
    finally:
        if engine is not None:
            await engine.dispose()
        if created:
            async with admin.begin() as connection:
                await connection.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        await admin.dispose()
    report = {"generated_at":datetime.now(timezone.utc).isoformat(), "postgres_version":postgres_version,
              "checks":checks, "provider_calls":0,
              "test_schema_removed":True, "code_sha256":{name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in (
                  "app/services/graph_retry_policy.py", "app/services/upload_service.py", "app/services/ingestion_service.py",
                  "app/services/document_processing_service.py", "app/schemas/extraction.py", "app/api/endpoints/ingest.py",
                  "scripts/evaluate_graph_retry.py")},
              "limitations":["Real PostgreSQL and ASGI endpoints, with source-existence and enqueue test doubles.",
                             "A separate process enforces the persisted deadline; no real API/ingestion-worker or hard-kill test.",
                             "Auth and middleware are covered by the normal suite; this isolated ASGI app tests retry endpoints only.",
                             "Committed vector/graph counts are authored fixtures; no Qdrant, Neo4j or R2 mutation or consistency claim.",
                             "Automatic delayed graph continuation is not implemented; this increment prevents premature manual retries."]}
    output = ROOT/args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.with_suffix(".json").write_text(json.dumps(report, indent=2)+"\n")
    output.with_suffix(".md").write_text("# Durable graph retry eligibility check\n\n"+
        "Real isolated PostgreSQL and retry API; no provider calls.\n\n"+
        "\n".join(f"- {name}: passed" for name in checks)+"\n\n## Limits\n\n"+
        "\n".join(f"- {value}" for value in report["limitations"])+"\n")
    print(json.dumps({"checks_passed":len(checks), "provider_calls":0, "test_schema_removed":True}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=55432)
    parser.add_argument("--output", default="evaluation/graph-retry-current")
    parser.add_argument("--child-check", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--schema", help=argparse.SUPPRESS)
    parser.add_argument("--upload-id", help=argparse.SUPPRESS)
    args = parser.parse_args()
    asyncio.run(child_check(args) if args.child_check else run(args))
