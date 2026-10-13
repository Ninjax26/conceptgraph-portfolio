"""Real isolated PostgreSQL READY/snapshot atomicity. All indexed counts are fixtures.

No provider/storage/vector/graph calls; no automatic dispatch or projection test.
"""

import argparse
import asyncio
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker
from app.core import database
from app.core.processing import ProcessingStage
from app.models.document_upload import Course, DocumentUpload, GraphContinuationJob, ProcessingAttempt
from app.services.course_service import CourseService
from app.services.graph_continuation_service import GraphContinuationService
from app.services.upload_service import UploadService
from app.services.parser_service import DocumentChunk
from scripts.evaluate_provider_admission import engine_for
from scripts.evaluate_graph_continuation_jobs import configuration


async def run(args):
    admin, engine, created = engine_for(args), None, False
    schema, checks = "cg_admission_" + uuid4().hex, {}
    uploads = UploadService()
    try:
        async with admin.begin() as connection:
            version = (await connection.execute(text("SHOW server_version"))).scalar_one()
            await connection.execute(text(f"CREATE SCHEMA {schema}"))
            created = True
        engine = engine_for(args, schema)
        sessions = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        with patch.object(database, "postgres_engine", engine):
            await database.initialize_database_schema()
        async with sessions() as session:
            course = Course(id=str(uuid4()), normalized_name="atomic staging fixture", display_name="Atomic staging fixture")
            session.add(course)
            await session.commit()

        async def fixture():
            upload_id, task = str(uuid4()), str(uuid4())
            async with sessions() as session:
                await uploads.create_upload(session, upload_id=upload_id, task_id=task, course=course,
                    content_hash="a"*64, original_filename="atomic-fixture.pdf", storage_key=f"fixture/{upload_id}",
                    page_count=1, source_bytes=100, reserved_chunk_count=1)
                assert await uploads.claim_for_processing(session, upload_id=upload_id, task_id=task,
                    lease_owner="fixture-worker", lease_seconds=180)
                assert await uploads.set_stage(session, upload_id, task, ProcessingStage.GRAPH_BUILT, lease_owner="fixture-worker")
            chunk = DocumentChunk(id=upload_id+":1:0", text="DNS resolves a domain name to an IP address.", metadata={
                "chunk_id":upload_id+":1:0", "document_id":course.id, "upload_id":upload_id, "execution_token":task,
                "document_name":"atomic-fixture.pdf", "page_number":1, "section_heading":"DNS"})
            result = {"chunks_indexed":1, "nodes_upserted":1, "relationships_upserted":0,
                "graph_status":"GRAPH_PARTIAL", "graph_batches_failed":1,
                "graph_provider_limited":True, "graph_retry_after_seconds":120}
            return upload_id, task, [chunk], result

        async def inspect(upload_id):
            async with sessions() as session:
                record = await uploads.get_upload(session, upload_id)
                attempt = (await session.execute(select(ProcessingAttempt).where(ProcessingAttempt.document_id == upload_id))).scalar_one()
                job = (await session.execute(select(GraphContinuationJob).where(GraphContinuationJob.upload_id == upload_id))).scalar_one_or_none()
                return record, attempt, job

        with ExitStack() as stack:
            configuration(stack)
            upload_id, task, chunks, result = await fixture()
            async with sessions() as session:
                assert await uploads.mark_completed(session, upload_id, task, result, lease_owner="fixture-worker")
            record, attempt, job = await inspect(upload_id)
            assert record.stage == attempt.stage == "READY" and job is None
            checks["default_completion_does_not_create_job"] = True

            atomic_id, atomic_task, atomic_chunks, atomic_result = await fixture()
            original_stage = GraphContinuationService.stage_for_ready
            async def inspect_before_commit(service, session, **kwargs):
                job = await original_stage(service, session, **kwargs)
                assert job is not None
                await session.flush()  # Force both writes; still invisible to another connection.
                outside_record, outside_attempt, outside_job = await inspect(atomic_id)
                assert outside_record.stage == outside_attempt.stage == "GRAPH_BUILT" and outside_job is None
                return job
            with patch.object(GraphContinuationService, "stage_for_ready", inspect_before_commit):
                async with sessions() as session:
                    assert await uploads.mark_completed(session, atomic_id, atomic_task, atomic_result,
                        lease_owner="fixture-worker", graph_continuation_chunks=atomic_chunks)
            record, attempt, job = await inspect(atomic_id)
            assert record.stage == attempt.stage == "READY" and job.status == "pending"
            assert record.task_id == job.document_task_id == atomic_task and record.lease_owner is None
            assert job.snapshot_json["chunks"][0]["metadata"]["execution_token"] == atomic_task
            assert job.next_attempt_at >= datetime.fromisoformat(record.result_json["graph_retry_not_before"])
            async with sessions() as session:
                context = await CourseService().get_ready_context(session, course.id)
                assert atomic_id in context.document_ids
            checks["ready_attempt_and_snapshot_become_visible_together"] = True
            checks["ready_identity_scope_and_persisted_reset_preserved"] = True

            failure_id, failure_task, failure_chunks, failure_result = await fixture()
            async def fail_after_flush(service, session, **kwargs):
                await original_stage(service, session, **kwargs)
                await session.flush()
                raise RuntimeError("Injected failure before commit")
            with patch.object(GraphContinuationService, "stage_for_ready", fail_after_flush):
                try:
                    async with sessions() as session:
                        await uploads.mark_completed(session, failure_id, failure_task, failure_result,
                            lease_owner="fixture-worker", graph_continuation_chunks=failure_chunks)
                except RuntimeError:
                    pass
                else:
                    raise AssertionError("Injected completion failure was not raised")
            record, attempt, job = await inspect(failure_id)
            assert record.stage == attempt.stage == "GRAPH_BUILT" and record.status == "active" and job is None
            assert record.lease_owner == "fixture-worker" and record.result_json is None
            checks["failure_after_flush_rolls_back_ready_attempt_and_job"] = True
            async with sessions() as session:
                assert await uploads.mark_completed(session, failure_id, failure_task, failure_result,
                    lease_owner="fixture-worker", graph_continuation_chunks=failure_chunks)
            record, attempt, job = await inspect(failure_id)
            assert record.stage == attempt.stage == "READY" and job is not None
            checks["same_fenced_execution_can_complete_after_rollback"] = True

            invalid_id, invalid_task, invalid_chunks, invalid_result = await fixture()
            invalid_chunks[0].metadata["execution_token"] = "foreign-task"
            try:
                async with sessions() as session:
                    await uploads.mark_completed(session, invalid_id, invalid_task, invalid_result,
                        lease_owner="fixture-worker", graph_continuation_chunks=invalid_chunks)
            except ValueError:
                pass
            else:
                raise AssertionError("Foreign source snapshot accepted")
            record, attempt, job = await inspect(invalid_id)
            assert record.stage == attempt.stage == "GRAPH_BUILT" and job is None
            checks["invalid_source_snapshot_cannot_partially_commit_ready"] = True

            superseded_id, superseded_task, superseded_chunks, superseded_result = await fixture()
            async with sessions() as session:
                assert not await uploads.mark_completed(session, superseded_id, superseded_task, superseded_result,
                    lease_owner="wrong-owner", graph_continuation_chunks=superseded_chunks)
            record, attempt, job = await inspect(superseded_id)
            assert record.stage == attempt.stage == "GRAPH_BUILT" and job is None
            checks["existing_ingestion_lease_fence_precedes_snapshot_staging"] = True

            budget_id, budget_task, budget_chunks, budget_result = await fixture()
            budget_result = {**budget_result, "graph_provider_limited":False, "graph_extraction_budget_applied":True}
            async with sessions() as session:
                assert await uploads.mark_completed(session, budget_id, budget_task, budget_result,
                    lease_owner="fixture-worker", graph_continuation_chunks=budget_chunks)
            record, attempt, job = await inspect(budget_id)
            assert record.stage == attempt.stage == "READY" and job is None
            checks["demo_batch_budget_does_not_stage_provider_job"] = True

            with patch.object(database, "postgres_engine", engine):
                await database.initialize_database_schema()
            async with sessions() as session:
                assert (await session.execute(select(func.count()).select_from(GraphContinuationJob))).scalar_one() == 2
            checks["restart_preserves_atomically_staged_jobs"] = True
    finally:
        if engine is not None:
            await engine.dispose()
        if created:
            async with admin.begin() as connection:
                await connection.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        await admin.dispose()
    sources = ("app/core/config.py", "app/services/upload_service.py", "app/services/document_processing_service.py",
        "app/services/graph_continuation_service.py", "scripts/evaluate_graph_snapshot_staging.py", "tests/test_graph_snapshot_staging.py")
    report = {"generated_at_utc":datetime.now(timezone.utc).isoformat(), "kind":"isolated READY/snapshot atomicity checks",
        "postgres_version":version, "checks":checks, "checks_passed":len(checks), "hosted_provider_calls":0,
        "production_test_data_mutations":0, "test_schema_removed":True,
        "code_sha256":{path:hashlib.sha256((ROOT/path).read_bytes()).hexdigest() for path in sources},
        "limitations":["Committed index/source/graph counts are fixtures; no full processor, provider or cross-store consistency test.",
            "Staging flag defaults off; dispatcher, graph-only worker and atomic graph publication remain unimplemented.",
            "New snapshot storage/retention budgets and full split-worker/hard-kill checks remain acceptance gates.",
            "Rollback is an injected pre-commit failure, not a simulation of an ambiguous network failure after commit."]}
    output = ROOT / "evaluation/graph-snapshot-staging-current"
    output.with_suffix(".json").write_text(json.dumps(report, indent=2)+"\n")
    output.with_suffix(".md").write_text("# READY/snapshot staging — isolated transaction checks\n\n"
        "Command: `.venv/bin/python scripts/evaluate_graph_snapshot_staging.py --socket-dir <private-test-socket>`.\n\n"
        + f"{len(checks)} checks passed. Hosted provider calls: 0. Production test-data mutations: 0.\n\n"
        + "\n".join(f"- {name}: passed" for name in checks) + "\n\n## Limits\n\n"
        + "\n".join(f"- {item}" for item in report["limitations"]) + "\n")
    print(json.dumps({"checks_passed":len(checks), "hosted_provider_calls":0, "test_schema_removed":True}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=55432)
    args = parser.parse_args()
    asyncio.run(run(args))
