"""Isolated PostgreSQL graph-job fencing checks. No graph generation/projection.

Separate claim processes and one killed claimant exercise metadata leases only;
they are NOT real API/ingestion workers or a cross-store consistency benchmark.
"""

import argparse
import asyncio
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker
from app.core import database
from app.core.config import Settings
from app.models.document_upload import Course, DocumentUpload, GraphContinuationJob, GraphExtractionCheckpoint
from app.schemas.extraction import ConceptNode, GraphExtractionResponse
from app.services import graph_continuation_service as jobs
from app.services import ingestion_service as ingestion
from app.services.course_service import CourseService
from app.services.upload_service import UploadService
from app.services.parser_service import DocumentChunk
from scripts.evaluate_provider_admission import engine_for
from scripts.evaluate_graph_retry import fixture


def configuration(stack):
    cfg = Settings(_env_file=None, RERANK_PROVIDER="local", LLM_PROVIDER="groq",
        GROQ_API_KEY="fixture-key", GEMINI_API_KEY="fixture-backup-key",
        GROQ_MODEL="fixture-model", GEMINI_MODEL="fixture-backup-model")
    stack.enter_context(patch.object(jobs, "settings", cfg))
    stack.enter_context(patch.object(ingestion, "settings", cfg))


async def child(args):
    engine = engine_for(args, args.schema)
    try:
        sessions = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        with ExitStack() as stack:
            configuration(stack)
            async with sessions() as session:
                claim = await jobs.GraphContinuationService().claim_next(session, lease_owner=f"claimant-{os.getpid()}", lease_seconds=180)
            print(json.dumps({"pid": os.getpid(), "claimed": claim is not None}), flush=True)
            if args.hold and claim is not None:
                await asyncio.Event().wait()
    finally:
        await engine.dispose()


async def process(args, schema, *, hold=False):
    command = [sys.executable, str(Path(__file__).resolve()), "--child", "--socket-dir", str(args.socket_dir),
               "--port", str(args.port), "--schema", schema]
    if hold:
        command.append("--hold")
    return await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)


async def one_claimant(args, schema):
    child_process = await process(args, schema)
    try:
        stdout, stderr = await asyncio.wait_for(child_process.communicate(), 30)
        assert child_process.returncode == 0, "Isolated claim process failed"
        return json.loads(stdout)
    finally:
        if child_process.returncode is None:
            child_process.kill()
            await child_process.wait()


async def make_due(sessions, upload_id, *, expire_lease=False):
    async with sessions() as session:
        job = (await session.execute(select(GraphContinuationJob).where(GraphContinuationJob.upload_id == upload_id))).scalar_one()
        now = (await session.execute(select(func.clock_timestamp()))).scalar_one()
        if expire_lease:
            job.lease_expires_at = now - timedelta(seconds=1)
        else:
            job.next_attempt_at = now - timedelta(seconds=1)
        await session.commit()


async def run(args):
    started = time.perf_counter()
    admin, engine, created = engine_for(args), None, False
    schema = "cg_admission_" + uuid4().hex
    checks = {}
    service = jobs.GraphContinuationService()
    try:
        async with admin.begin() as connection:
            version = (await connection.execute(text("SHOW server_version"))).scalar_one()
            encoding = (await connection.execute(text("SHOW server_encoding"))).scalar_one()
            await connection.execute(text(f"CREATE SCHEMA {schema}"))
            created = True
        engine = engine_for(args, schema)
        sessions = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        with patch.object(database, "postgres_engine", engine):
            await database.initialize_database_schema()
            await database.initialize_database_schema()
        async with sessions() as session:
            indexes = (await session.execute(text("SELECT indexname FROM pg_indexes WHERE schemaname=:schema AND tablename='graph_continuation_jobs'"), {"schema": schema})).scalars().all()
            assert "ix_graph_job_due" in indexes and "ix_graph_job_lease" in indexes
            course = Course(id=str(uuid4()), normalized_name="graph job fixture", display_name="Graph job fixture")
            session.add(course)
            await session.commit()
        checks["additive_schema_restart_and_due_lease_indexes"] = True

        async def prepared():
            upload_id, task, checkpoint = await fixture(sessions, course)
            async with sessions() as session:
                record = await UploadService().get_upload(session, upload_id)
                record.result_json = {**record.result_json, "graph_provider_limited": True}
                await session.commit()
                original = dict(record.result_json)
                chunks = [DocumentChunk(id=f"{upload_id}:{index}", text=f"DNS source evidence {index}.", metadata={
                    "chunk_id": f"{upload_id}:{index}", "document_id": course.id, "upload_id": upload_id,
                    "document_name": record.original_filename, "execution_token": task,
                    "page_number": 1, "section_heading": "DNS"}) for index in range(3)]
                job = await service.schedule(session, upload_id=upload_id, document_task_id=task,
                    chunks=chunks, retry_after_seconds=1)
                assert job is not None
                assert job.next_attempt_at >= datetime.fromisoformat(original["graph_retry_not_before"])
            return upload_id, task, chunks, original

        with ExitStack() as stack:
            configuration(stack)
            upload_id, task, chunks, original = await prepared()
            for mutation in ("status='running'", "attempt_count=max_attempts+1"):
                async with sessions() as session:
                    try:
                        await session.execute(text(f"UPDATE graph_continuation_jobs SET {mutation} WHERE upload_id=:upload"), {"upload": upload_id})
                        await session.commit()
                    except IntegrityError:
                        await session.rollback()
                    else:
                        raise AssertionError("Database accepted an invalid lease/attempt state")
            checks["database_rejects_incomplete_running_lease_and_excess_attempts"] = True
            async with sessions() as session:
                assert await service.claim_next(session, lease_owner="early", lease_seconds=180) is None
                job = (await session.execute(select(GraphContinuationJob).where(GraphContinuationJob.upload_id == upload_id))).scalar_one()
                due, identifier = job.next_attempt_at, job.id
                duplicate = await service.schedule(session, upload_id=upload_id, document_task_id=task,
                    chunks=chunks, retry_after_seconds=10000)
                assert duplicate.id == identifier and duplicate.next_attempt_at == due and duplicate.attempt_count == 0
            checks["persisted_delay_and_idempotent_scheduling_no_hot_retry"] = True

            await make_due(sessions, upload_id)
            children = await asyncio.gather(*(one_claimant(args, schema) for _ in range(3)))
            assert sum(child_result["claimed"] for child_result in children) == 1
            assert len({child_result["pid"] for child_result in children}) == 3
            async with sessions() as session:
                record = await UploadService().get_upload(session, upload_id)
                job = (await session.execute(select(GraphContinuationJob).where(GraphContinuationJob.upload_id == upload_id))).scalar_one()
                claim = jobs.GraphJobClaim(job.id, upload_id, task, job.claim_token, job.lease_owner,
                    jobs.snapshot_chunks(job.snapshot_json, record))
                assert job.attempt_count == 1 and record.task_id == task and record.result_json == original
                assert record.status == "ready" and record.stage == "READY" and record.lease_owner is None
                context = await CourseService().get_ready_context(session, course.id)
                assert upload_id in context.document_ids and task in {document.task_id for document in context.documents}
            checks["three_processes_one_claim_with_ready_identity_and_scope_unchanged"] = True

            batch = ingestion.IngestionService._graph_section_batches(chunks, batch_size=3)[0]
            graph = GraphExtractionResponse(nodes=[ConceptNode(id="dns", name="DNS", type="concept", source_chunk_id=chunks[0].id),
                ConceptNode(id="ip", name="IP address", type="concept", source_chunk_id=chunks[1].id)])
            async with sessions() as session:
                assert not await service.heartbeat(session, replace(claim, claim_token="wrong"), lease_seconds=180)
                assert not await service.save_section_checkpoint(session, replace(claim, lease_owner="wrong"), batch=batch, extraction=graph)
                assert await service.save_section_checkpoint(session, claim, batch=batch, extraction=graph)
                checkpoint = (await session.execute(select(GraphExtractionCheckpoint).where(
                    GraphExtractionCheckpoint.upload_id == upload_id, GraphExtractionCheckpoint.batch_key == batch.batch_key))).scalar_one()
                assert checkpoint.extraction_json["nodes"][0]["upload_id"] == upload_id
            checks["independent_claim_token_and_owner_fence_checkpoint_writes"] = True

            recovery_batch = ingestion.IngestionService.recovery_checkpoint_batch(chunks, {batch.batch_key:graph})
            assert recovery_batch is not None
            async with sessions() as session:
                assert await service.save_checkpoint(session, claim, batch=recovery_batch, extraction=GraphExtractionResponse(empty_extraction=True))
                try:
                    await service.save_checkpoint(session, claim, batch=replace(recovery_batch, batch_key="global:v5:"+"0"*64), extraction=graph)
                except ValueError:
                    await session.rollback()
                else:
                    raise AssertionError("Forged recovery identity accepted")
                assert not await service.save_checkpoint(session, replace(claim, claim_token="stale"), batch=recovery_batch, extraction=graph)
            checks["recovery_checkpoint_is_source_concept_and_claim_fenced"] = True

            async with sessions() as session:
                assert await service.load_checkpoints(session, replace(claim, claim_token="stale")) is None
                completed = await service.load_checkpoints(session, replace(claim, chunks=()))
                assert set(completed) == {batch.batch_key, recovery_batch.batch_key}
                assert not session.in_transaction()
                assert completed[batch.batch_key].nodes[0].upload_id == upload_id
            checks["fenced_read_uses_persisted_snapshot_filters_legacy_and_releases_locks"] = True
            extractor = ingestion.IngestionService(SimpleNamespace(), SimpleNamespace())
            extractor.extract_graph_from_text = AsyncMock(side_effect=AssertionError("Unexpected provider call"))
            resumed = await extractor.extract_graph_from_chunks(chunks, completed_batches=completed)
            extractor.extract_graph_from_text.assert_not_awaited()
            assert resumed.global_linking_processed and len(resumed.nodes) == 2
            checks["persisted_empty_recovery_and_sections_resume_without_provider_calls"] = True

            changed_graph = graph.model_copy(update={"nodes":[graph.nodes[0].model_copy(update={"name":"Domain Name System"}), graph.nodes[1]]})
            async with sessions() as session:
                assert await service.save_section_checkpoint(session, claim, batch=batch, extraction=changed_graph)
                changed_completed = await service.load_checkpoints(session, claim)
                assert recovery_batch.batch_key not in changed_completed
                try:
                    await service.save_checkpoint(session, claim, batch=recovery_batch, extraction=graph)
                except ValueError:
                    await session.rollback()
                else:
                    raise AssertionError("Recovery identity from older concepts accepted")
                assert await service.save_section_checkpoint(session, claim, batch=batch, extraction=graph)
            checks["changed_section_concepts_invalidate_recovery_save"] = True
            checks["changed_section_concepts_invalidate_recovery_read"] = True

            async with sessions() as session:
                assert await service.defer(session, claim, retry_after_seconds=90)
                assert not await service.heartbeat(session, claim, lease_seconds=180)
                assert await service.claim_next(session, lease_owner="too-early", lease_seconds=180) is None
                job = (await session.execute(select(GraphContinuationJob).where(GraphContinuationJob.upload_id == upload_id))).scalar_one()
                now = (await session.execute(select(func.clock_timestamp()))).scalar_one()
                assert 85 <= (job.next_attempt_at - now).total_seconds() <= 90
                assert job.claim_token is None and job.attempt_count == 1
                assert (await UploadService().get_upload(session, upload_id)).result_json == original
            checks["defer_persists_reset_and_invalidates_old_claim_without_changing_ready_result"] = True

            await make_due(sessions, upload_id)
            killed = await process(args, schema, hold=True)
            try:
                result = json.loads(await asyncio.wait_for(killed.stdout.readline(), 30))
                assert result["claimed"]
                killed.kill()
                await killed.wait()
                assert killed.returncode == -9
            finally:
                if killed.returncode is None:
                    killed.kill()
                    await killed.wait()
            await make_due(sessions, upload_id, expire_lease=True)  # accelerated fixture clock, not a natural 180s wait
            async with sessions() as session:
                recovered = await service.claim_next(session, lease_owner="recovered", lease_seconds=180)
                assert recovered is not None and recovered.claim_token != claim.claim_token
                assert not await service.save_section_checkpoint(session, claim, batch=batch, extraction=graph)
                assert await service.heartbeat(session, recovered, lease_seconds=180)
                assert await service.save_section_checkpoint(session, recovered, batch=batch, extraction=graph)
                assert len(await UploadService().load_graph_checkpoints(session, upload_id)) == 3  # fixture + section + recovery
                job = (await session.execute(select(GraphContinuationJob).where(GraphContinuationJob.upload_id == upload_id))).scalar_one()
                job.max_attempts = job.attempt_count
                await session.commit()
                assert await service.defer(session, recovered, retry_after_seconds=1)
                job = (await session.execute(select(GraphContinuationJob).where(GraphContinuationJob.upload_id == upload_id))).scalar_one()
                assert job.status == "stopped" and job.failure_category == "attempt_limit"
                stopped_attempts = job.attempt_count
                assert await service.claim_next(session, lease_owner="exhausted", lease_seconds=180) is None
                scheduled_again = await service.schedule(session, upload_id=upload_id, document_task_id=task,
                    chunks=chunks, retry_after_seconds=1)
                assert scheduled_again.status == "stopped" and scheduled_again.attempt_count == stopped_attempts
            checks["killed_claimant_recovers_after_fixture_lease_expiry_and_retains_checkpoint"] = True
            checks["attempt_limit_stops_automatic_requeue"] = True
            checks["duplicate_scheduling_cannot_restart_exhausted_job"] = True

            invalid_id, _, _, _ = await prepared()
            async with sessions() as session:
                job = (await session.execute(select(GraphContinuationJob).where(GraphContinuationJob.upload_id == invalid_id))).scalar_one()
                payload = json.loads(json.dumps(job.snapshot_json))
                payload["chunks"][0]["text"] = "Corrupted snapshot text."
                job.snapshot_json = payload
                await session.commit()
            await make_due(sessions, invalid_id)
            async with sessions() as session:
                assert await service.claim_next(session, lease_owner="invalid", lease_seconds=180) is None
                job = (await session.execute(select(GraphContinuationJob).where(GraphContinuationJob.upload_id == invalid_id))).scalar_one()
                assert job.status == "stopped" and job.failure_category == "incompatible_snapshot"
            checks["tampered_snapshot_is_stopped_without_reparsing_or_provider_calls"] = True

            model_id, _, _, _ = await prepared()
            await make_due(sessions, model_id)
            with patch.object(ingestion.settings, "groq_model", "changed-model"):
                async with sessions() as session:
                    assert await service.claim_next(session, lease_owner="changed-model", lease_seconds=180) is None
                    job = (await session.execute(select(GraphContinuationJob).where(GraphContinuationJob.upload_id == model_id))).scalar_one()
                    assert job.failure_category == "incompatible_snapshot"
            checks["changed_model_contract_requires_explicit_rebuild"] = True

            stale_id, _, _, _ = await prepared()
            async with sessions() as session:
                record = await UploadService().get_upload(session, stale_id)
                record.task_id = str(uuid4())
                await session.commit()
            await make_due(sessions, stale_id)
            async with sessions() as session:
                assert await service.claim_next(session, lease_owner="stale", lease_seconds=180) is None
                job = (await session.execute(select(GraphContinuationJob).where(GraphContinuationJob.upload_id == stale_id))).scalar_one()
                assert job.failure_category == "superseded"
            checks["superseded_document_task_stops_old_job"] = True

            expired_id, _, _, _ = await prepared()
            async with sessions() as session:
                record = await UploadService().get_upload(session, expired_id)
                now = (await session.execute(select(func.clock_timestamp()))).scalar_one()
                record.created_at = now - timedelta(days=31)
                await session.commit()
            await make_due(sessions, expired_id)
            async with sessions() as session:
                assert await service.claim_next(session, lease_owner="retention", lease_seconds=180) is None
                job = (await session.execute(select(GraphContinuationJob).where(GraphContinuationJob.upload_id == expired_id))).scalar_one()
                assert job.failure_category == "retention_expired"
            checks["expired_retention_is_not_resurrected"] = True

            deletion_id, _, _, _ = await prepared()
            await make_due(sessions, deletion_id)
            async with sessions() as session:
                assert await UploadService().lock_for_deletion(session, deletion_id) is not None
                outside = await one_claimant(args, schema)
                assert not outside["claimed"]
                await session.rollback()
                deletion_claim = await service.claim_next(session, lease_owner="deletion-test", lease_seconds=180)
                assert deletion_claim is not None and deletion_claim.upload_id == deletion_id
                assert await UploadService().lock_for_deletion(session, deletion_id) is not None
                assert await UploadService().delete_document(session, deletion_id) is not None
                assert not await service.heartbeat(session, deletion_claim, lease_seconds=180)
                assert not await service.save_section_checkpoint(session, deletion_claim, batch=batch, extraction=graph)
                assert (await session.execute(select(func.count()).select_from(GraphContinuationJob).where(GraphContinuationJob.upload_id == deletion_id))).scalar_one() == 0
                assert not await UploadService().load_graph_checkpoints(session, deletion_id)
            checks["deletion_lock_skipped_and_fk_cascade_fences_old_claim"] = True

            budget_id, budget_task, _ = await fixture(sessions, course, delay=None)
            async with sessions() as session:
                assert await service.schedule(session, upload_id=budget_id, document_task_id=budget_task,
                    chunks=chunks, retry_after_seconds=1) is None
                assert (await session.execute(select(func.count()).select_from(GraphContinuationJob).where(GraphContinuationJob.upload_id == budget_id))).scalar_one() == 0
            checks["batch_budget_only_result_does_not_schedule_provider_retry"] = True

            async with sessions() as session:
                # Old-release SQL still sees unchanged document/evidence columns.
                count = (await session.execute(text("SELECT count(*) FROM document_uploads WHERE stage='READY' AND status='ready'"))).scalar_one()
                assert count == 6
            with patch.object(database, "postgres_engine", engine):
                await database.initialize_database_schema()
            async with sessions() as session:
                assert (await session.execute(select(func.count()).select_from(GraphContinuationJob))).scalar_one() == 5
            checks["old_document_sql_and_schema_restart_preserve_jobs"] = True
    finally:
        if engine is not None:
            await engine.dispose()
        if created:
            async with admin.begin() as connection:
                await connection.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        await admin.dispose()
    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "kind": "isolated PostgreSQL graph-job metadata contracts", "postgres_version": version, "server_encoding": encoding,
        "checks": checks, "checks_passed": len(checks), "hosted_provider_calls": 0, "production_test_data_mutations": 0,
        "test_schema_removed": True, "duration_seconds": round(time.perf_counter() - started, 4),
        "code_sha256": {path: hashlib.sha256((ROOT/path).read_bytes()).hexdigest() for path in (
            "app/models/document_upload.py", "app/services/graph_continuation_service.py", "scripts/evaluate_graph_continuation_jobs.py",
            "tests/test_graph_continuation_snapshot.py", "tests/test_graph_job_checkpoint_reads.py",
            "app/services/ingestion_service.py", "app/services/upload_service.py")},
        "limitations": [
            "Three fresh claim processes and a SIGKILL claimant test metadata leases only, not actual API/ingestion-worker operation.",
            "Killed-process recovery uses an explicit expired-lease fixture, not a natural lease-duration wait.",
            "Source/vector/graph counts and extracted graph output are authored fixtures; no Qdrant, Neo4j or R2 consistency claim.",
            "Scheduler/graph-only worker/recovery projection are not connected; no production automatic continuation is enabled.",
            "Snapshot JSON is bounded at 8 MiB but its future installation-wide storage accounting still needs integration.",
            "Observed test duration is not an ingestion throughput, semantic-quality or production capacity metric.",
        ],
    }
    output = ROOT / "evaluation/graph-continuation-jobs-current"
    output.with_suffix(".json").write_text(json.dumps(report, indent=2)+"\n")
    output.with_suffix(".md").write_text("# Graph continuation jobs — isolated metadata checks\n\n"
        "Command: `.venv/bin/python scripts/evaluate_graph_continuation_jobs.py --socket-dir <private-test-socket>`.\n\n"
        + f"{len(checks)} checks passed. Hosted provider calls: 0. Production test-data mutations: 0.\n\n"
        + "\n".join(f"- {name}: passed" for name in checks) + "\n\n## Limits\n\n"
        + "\n".join(f"- {item}" for item in report["limitations"]) + "\n")
    print(json.dumps({"checks_passed": len(checks), "hosted_provider_calls": 0, "test_schema_removed": True}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=55432)
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--hold", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--schema", help=argparse.SUPPRESS)
    args = parser.parse_args()
    asyncio.run(child(args) if args.child else run(args))
