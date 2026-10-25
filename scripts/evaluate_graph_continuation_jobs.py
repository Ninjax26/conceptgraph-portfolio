"""Isolated PostgreSQL graph-job/stage checks. No hosted generation/projection.

Separate claim processes and one killed extraction-stage process use mocked AI;
they are NOT full API/ingestion workers or a cross-store consistency benchmark.
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

from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker
from app.core import database
from app.core.config import Settings
from app.models.document_upload import Course, DocumentUpload, GraphContinuationJob, GraphExtractionCheckpoint, GraphPublicationDraft, GraphPublicationIntent
from app.schemas.extraction import ConceptNode, GraphExtractionResponse
from app.services import graph_continuation_service as jobs
from app.services import ingestion_service as ingestion
from app.services.course_service import CourseService
from app.services.upload_service import UploadService
from app.services.parser_service import DocumentChunk
from app.services.graph_job_extraction import GraphJobDeferred, GraphJobExtraction
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
            if args.hold and claim is not None:
                async def pause(chunks, *, completed_batches, on_batch_completed):
                    print(json.dumps({"pid": os.getpid(), "claimed": True, "stage_started": True,
                        "chunks_loaded": len(chunks), "checkpoints_loaded": len(completed_batches)}), flush=True)
                    await asyncio.Event().wait()
                await GraphJobExtraction(sessions, SimpleNamespace(extract_graph_from_chunks=pause)).extract(claim)
            else:
                print(json.dumps({"pid": os.getpid(), "claimed": claim is not None}), flush=True)
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
                inputs = await service.load_input(session, replace(claim, chunks=()))
                assert inputs is not None
                assert inputs.chunks == tuple(chunks)
                completed = inputs.checkpoints
                assert set(completed) == {batch.batch_key, recovery_batch.batch_key}
                assert not session.in_transaction()
                assert completed[batch.batch_key].nodes[0].upload_id == upload_id
            checks["fenced_read_uses_persisted_snapshot_filters_legacy_and_releases_locks"] = True
            extractor = ingestion.IngestionService(SimpleNamespace(), SimpleNamespace())
            extractor.extract_graph_from_text = AsyncMock(side_effect=AssertionError("Unexpected provider call"))
            resumed = await extractor.extract_graph_from_chunks(inputs.chunks, completed_batches=completed)
            extractor.extract_graph_from_text.assert_not_awaited()
            assert resumed.global_linking_processed and len(resumed.nodes) == 2
            checks["persisted_empty_recovery_and_sections_resume_without_provider_calls"] = True
            stage_result = await GraphJobExtraction(sessions, extractor).extract(replace(claim, chunks=()))
            extractor.extract_graph_from_text.assert_not_awaited()
            assert stage_result.global_linking_processed and len(stage_result.nodes) == 2
            checks["graph_only_stage_renews_real_lease_and_resumes_saved_batches"] = True
            async with sessions() as session:
                candidate = await service.prepare_publication(session, replace(claim, chunks=()), stage_result)
                assert candidate is not None and len(candidate.sha256) == 64
                assert session.in_transaction()
                async with sessions() as other:
                    assert (await other.execute(select(DocumentUpload.upload_id).where(
                        DocumentUpload.upload_id == upload_id).with_for_update(skip_locked=True))).scalar_one_or_none() is None
                await session.rollback()
                async with sessions() as other:
                    assert (await other.execute(select(DocumentUpload.upload_id).where(
                        DocumentUpload.upload_id == upload_id).with_for_update(skip_locked=True))).scalar_one() == upload_id
                assert await service.prepare_publication(session, replace(claim, claim_token="stale"), stage_result) is None
                assert not session.in_transaction()
            checks["publication_preflight_keeps_document_fence_and_rejects_stale_claim"] = True

            async with sessions() as session:
                restored = await service.restore_publication(session, replace(claim, chunks=()), candidate)
                assert restored.model_dump() == stage_result.model_dump()
                assert session.in_transaction()
                async with sessions() as other:
                    assert (await other.execute(select(DocumentUpload.upload_id).where(
                        DocumentUpload.upload_id == upload_id).with_for_update(skip_locked=True))).scalar_one_or_none() is None
                await session.rollback()
                async with sessions() as other:
                    assert (await other.execute(select(DocumentUpload.upload_id).where(
                        DocumentUpload.upload_id == upload_id).with_for_update(skip_locked=True))).scalar_one() == upload_id
                assert await service.restore_publication(session, replace(claim, claim_token="stale"), candidate) is None
                assert not session.in_transaction()
            checks["publication_replay_uses_current_sources_keeps_fence_and_rejects_stale_claim"] = True

            async with sessions() as session:
                assert await service.save_publication_draft(session, claim, candidate)
                assert await service.save_publication_draft(session, claim, candidate)
                assert await service.load_publication_draft(session, claim) == candidate
                await session.rollback()
            async with sessions() as session:
                assert await service.load_publication_draft(session, claim) is None
                await session.rollback()
                assert await service.save_publication_draft(session, claim, candidate)
                await session.commit()
            with patch.object(database, "postgres_engine", engine):
                await database.initialize_database_schema()
            async with sessions() as session:
                assert await service.load_publication_draft(session, replace(claim, chunks=())) == candidate
                assert await service.load_publication_draft(session, replace(claim, claim_token="stale")) is None
            checks["draft_same_transaction_idempotency_rollback_and_restart_preservation"] = True

            draft_bytes = len(candidate.payload_json.encode("utf-8"))
            for max_bytes, expected in ((draft_bytes*2, True), (draft_bytes*2-1, False)):
                with patch.object(jobs, "settings", jobs.settings.model_copy(update={
                        "graph_draft_max_jobs":2, "graph_draft_max_bytes":max_bytes})):
                    async with sessions() as session:
                        assert await service._draft_capacity(session, candidate) is expected
                        await session.rollback()
            with patch.object(jobs, "settings", jobs.settings.model_copy(update={"graph_draft_max_jobs":1})):
                async with sessions() as session:
                    assert not await service._draft_capacity(session, candidate)
                    assert await service.save_publication_draft(session, claim, candidate)
                    await session.rollback()
            checks["draft_logical_byte_and_count_limits_preserve_idempotent_saved_candidate"] = True

            contenders = []
            for index in range(2):
                contender_id, contender_task, _, contender_result = await prepared()
                await make_due(sessions, contender_id)
                async with sessions() as session:
                    contender_claim = await service.claim_next(session, lease_owner=f"draft-contender-{index}", lease_seconds=180)
                    assert contender_claim.upload_id == contender_id
                    contender_candidate = await service.prepare_publication(session, contender_claim,
                        GraphExtractionResponse(empty_extraction=True))
                    await session.rollback()
                contenders.append((contender_claim, contender_candidate, contender_result))
            start_admissions = asyncio.Event()
            async def save_contender(item):
                contender_claim, contender_candidate, _ = item
                async with sessions() as session:
                    await start_admissions.wait()
                    accepted = await service.save_publication_draft(session, contender_claim, contender_candidate)
                    await session.commit()
                    return accepted
            with patch.object(jobs, "settings", jobs.settings.model_copy(update={"graph_draft_max_jobs":2})):
                tasks = [asyncio.create_task(save_contender(item)) for item in contenders]
                start_admissions.set()
                assert sum(await asyncio.gather(*tasks)) == 1
            async with sessions() as session:
                assert (await session.execute(select(func.count()).select_from(GraphPublicationDraft))).scalar_one() == 2
                for contender_claim, _, contender_result in contenders:
                    assert (await UploadService().get_upload(session, contender_claim.upload_id)).result_json == contender_result
                # Remove only these authored disposable fixture documents;
                # their draft/job/checkpoint rows cascade inside this schema.
                await session.execute(delete(DocumentUpload).where(DocumentUpload.upload_id.in_(
                    [item[0].upload_id for item in contenders])))
                await session.commit()
            checks["two_concurrent_sessions_admit_one_draft_at_last_slot_without_ready_mutation"] = True

            async with sessions() as session:
                intent = await service.stage_publication_intent(session, claim)
                assert intent is not None and intent.payload_sha256 == candidate.sha256
                intent_id = intent.id
                assert (await service.stage_publication_intent(session, claim)).id == intent_id
                await session.commit()
                assert (await UploadService().get_upload(session, upload_id)).result_json == original
            async with sessions() as session:
                repeated = await service.stage_publication_intent(session, claim)
                assert repeated is not None and repeated.id == intent_id
                await session.rollback()
            with patch.object(database, "postgres_engine", engine):
                await database.initialize_database_schema()
            async with sessions() as session:
                saved = (await session.execute(select(GraphPublicationIntent).where(
                    GraphPublicationIntent.job_id == claim.job_id))).scalar_one()
                assert saved.id == intent_id and saved.document_task_id == task
            checks["intent_stages_only_from_validated_draft_and_survives_restart"] = True

            orphan_id, orphan_task, _, _ = await prepared()
            async with sessions() as session:
                orphan_job = (await session.execute(select(GraphContinuationJob).where(
                    GraphContinuationJob.upload_id == orphan_id))).scalar_one()
                session.add(GraphPublicationIntent(id=str(uuid4()), job_id=orphan_job.id,
                    upload_id=orphan_id, document_task_id=orphan_task,
                    course_id=course.id, payload_sha256="a" * 64))
                await session.commit()
                await session.execute(delete(DocumentUpload).where(DocumentUpload.upload_id == orphan_id))
                await session.commit()
            async with sessions() as session:
                assert (await session.execute(select(func.count()).select_from(GraphPublicationIntent).where(
                    GraphPublicationIntent.upload_id == orphan_id))).scalar_one() == 1
                assert (await session.execute(select(func.count()).select_from(GraphContinuationJob).where(
                    GraphContinuationJob.upload_id == orphan_id))).scalar_one() == 0
                assert await UploadService().get_upload(session, orphan_id) is None
            checks["intent_survives_document_and_job_cascade_for_future_reconciliation"] = True

            async with sessions() as session:
                changed = stage_result.model_copy(deep=True)
                changed.nodes[0].description = "Different validated candidate"
                alternative = await service.prepare_publication(session, claim, changed)
                try:
                    await service.save_publication_draft(session, claim, alternative)
                except ValueError as exc:
                    assert "different" in str(exc)
                    await session.rollback()
                else:
                    raise AssertionError("Existing draft was replaced")
                assert await service.load_publication_draft(session, claim) == candidate
                await session.rollback()
                try:
                    await session.execute(text("UPDATE graph_publication_drafts SET payload_json='' WHERE job_id=:job"), {"job":claim.job_id})
                    await session.commit()
                except IntegrityError:
                    await session.rollback()
                else:
                    raise AssertionError("Database accepted an empty draft")
            checks["draft_conflict_and_database_storage_constraint_fail_closed"] = True

            async with sessions() as session:
                await session.execute(text("DELETE FROM graph_continuation_jobs WHERE id=:job"), {"job":claim.job_id})
                assert (await session.execute(select(func.count()).select_from(GraphPublicationDraft))).scalar_one() == 0
                assert (await UploadService().get_upload(session, upload_id)).result_json == original
                await session.rollback()
                assert await service.load_publication_draft(session, claim) == candidate
            checks["job_deletion_cascades_unpublished_draft_without_changing_ready_result_and_rolls_back"] = True

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

            limited = SimpleNamespace(extract_graph_from_chunks=AsyncMock(return_value=GraphExtractionResponse(
                provider_limited=True, provider_retry_after_seconds=90)))
            try:
                await GraphJobExtraction(sessions, limited).extract(claim)
            except GraphJobDeferred:
                pass
            else:
                raise AssertionError("Quota-limited extraction returned a publishable result")
            limited.extract_graph_from_chunks.assert_awaited_once()
            checks["graph_only_stage_durably_defers_known_quota_without_hot_retry"] = True
            async with sessions() as session:
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
                assert result["stage_started"] and result["chunks_loaded"] == len(chunks)
                assert result["checkpoints_loaded"] == 2
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
                await session.rollback()
                recovered_graph = await GraphJobExtraction(sessions, extractor).extract(recovered)
                extractor.extract_graph_from_text.assert_not_awaited()
                assert len(recovered_graph.nodes) == 2 and recovered_graph.global_linking_processed
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
            checks["killed_extraction_stage_recovers_after_fixture_lease_expiry_and_reuses_checkpoints"] = True
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

            async with sessions() as session:
                used = (await session.execute(text(
                    "SELECT coalesce(sum(octet_length(snapshot_json::jsonb::text)),0) FROM graph_continuation_jobs"))).scalar_one()
                with patch.object(jobs, "settings", jobs.settings.model_copy(update={"graph_snapshot_max_bytes": used + 2})):
                    assert await service._snapshot_capacity(session, "not-an-upload", {})
                await session.rollback()
                with patch.object(jobs, "settings", jobs.settings.model_copy(update={"graph_snapshot_max_bytes": used + 1})):
                    assert not await service._snapshot_capacity(session, "not-an-upload", {})
                await session.rollback()
            checks["logical_jsonb_byte_budget_enforces_exact_boundary"] = True

            async def admission_candidate():
                candidate_id, candidate_task, _ = await fixture(sessions, course)
                async with sessions() as session:
                    record = await UploadService().get_upload(session, candidate_id)
                    record.result_json = {**record.result_json, "graph_provider_limited": True}
                    await session.commit()
                chunk = DocumentChunk(id=candidate_id+":0", text="Synthetic DNS source.", metadata={
                    "upload_id": candidate_id, "document_name": record.original_filename,
                    "chunk_id": candidate_id+":0", "document_id": course.id, "execution_token": candidate_task,
                    "page_number": 1, "section_heading": "DNS"})
                return candidate_id, candidate_task, [chunk]
            candidates = [await admission_candidate(), await admission_candidate()]
            async def admit(candidate):
                candidate_id, candidate_task, candidate_chunks = candidate
                async with sessions() as session:
                    return await service.schedule(session, upload_id=candidate_id, document_task_id=candidate_task,
                        chunks=candidate_chunks, retry_after_seconds=1)
            async with sessions() as session:
                retained = (await session.execute(select(func.count()).select_from(GraphContinuationJob))).scalar_one()
            with patch.object(jobs, "settings", jobs.settings.model_copy(update={"graph_snapshot_max_jobs": retained+1})):
                admitted = await asyncio.gather(*(admit(candidate) for candidate in candidates))
            assert sum(job is not None for job in admitted) == 1
            async with sessions() as session:
                assert (await session.execute(select(func.count()).select_from(GraphContinuationJob))).scalar_one() == retained+1
            checks["concurrent_snapshot_admission_cannot_exceed_retained_job_cap"] = True
            async with sessions() as session:
                terminal = (await session.execute(select(GraphContinuationJob).where(
                    GraphContinuationJob.upload_id == upload_id))).scalar_one()
                terminal.updated_at = datetime.now(timezone.utc) - timedelta(days=8)
                retained_attempts, retained_digest = terminal.attempt_count, terminal.snapshot_sha256
                await session.commit()
                assert await service.prune_terminal_snapshots(session, older_than_days=7, limit=1) == 1
                await session.rollback()
                terminal = (await session.execute(select(GraphContinuationJob).where(
                    GraphContinuationJob.upload_id == upload_id))).scalar_one()
                assert terminal.snapshot_json
                await session.rollback()
                assert await service.prune_terminal_snapshots(session, older_than_days=7, limit=1) == 1
                await session.commit()
                terminal = (await session.execute(select(GraphContinuationJob).where(
                    GraphContinuationJob.upload_id == upload_id))).scalar_one()
                assert terminal.snapshot_json == {} and terminal.attempt_count == retained_attempts
                assert terminal.status == "stopped" and terminal.snapshot_sha256 == retained_digest
                assert await service.prune_terminal_snapshots(session, older_than_days=7, limit=1) == 0
                duplicate = await service.schedule(session, upload_id=upload_id, document_task_id=task,
                    chunks=chunks, retry_after_seconds=1)
                assert duplicate.status == "stopped" and duplicate.attempt_count == retained_attempts
                assert duplicate.snapshot_json == {}
                assert len(await UploadService().load_graph_checkpoints(session, upload_id)) == 3
                assert (await UploadService().get_upload(session, upload_id)).result_json == original
            checks["terminal_snapshot_cleanup_rolls_back_and_preserves_exhausted_tombstone"] = True
            async with sessions() as session:
                # Snapshot maintenance refreshed updated_at; recent terminal
                # jobs must not be pruned. Accelerate only the fixture clock.
                assert await service.prune_terminal_drafts(session, older_than_days=7, limit=1) == 0
                terminal = (await session.execute(select(GraphContinuationJob).where(
                    GraphContinuationJob.upload_id == upload_id))).scalar_one()
                terminal.updated_at = datetime.now(timezone.utc) - timedelta(days=8)
                await session.commit()
                await session.execute(select(DocumentUpload).where(
                    DocumentUpload.upload_id == upload_id).with_for_update())
                async with sessions() as other:
                    assert await service.prune_terminal_drafts(other, older_than_days=7, limit=1) == 0
                    await other.rollback()
                await session.rollback()
                checks["terminal_draft_cleanup_skips_document_locked_by_another_session"] = True
                assert await service.prune_terminal_drafts(session, older_than_days=7, limit=1) == 1
                assert await service.prune_terminal_drafts(session, older_than_days=7, limit=1) == 0
                await session.rollback()
                assert (await session.execute(select(func.count()).select_from(GraphPublicationDraft))).scalar_one() == 1
                await session.rollback()
                assert await service.prune_terminal_drafts(session, older_than_days=7, limit=1) == 1
                await session.commit()
                assert (await session.execute(select(func.count()).select_from(GraphPublicationDraft))).scalar_one() == 0
                terminal = (await session.execute(select(GraphContinuationJob).where(
                    GraphContinuationJob.upload_id == upload_id))).scalar_one()
                assert terminal.status == "stopped" and terminal.attempt_count == retained_attempts
                assert terminal.snapshot_sha256 == retained_digest and terminal.snapshot_json == {}
                assert len(await UploadService().load_graph_checkpoints(session, upload_id)) == 3
                assert (await UploadService().get_upload(session, upload_id)).result_json == original
            checks["terminal_draft_cleanup_is_bounded_repeatable_rollback_safe_and_preserves_job_history"] = True
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
            "app/services/graph_job_extraction.py", "tests/test_graph_job_extraction.py",
            "app/core/config.py", "tests/test_graph_snapshot_budget.py",
            "app/services/graph_publication_candidate.py", "tests/test_graph_publication_candidate.py",
            "app/services/ingestion_service.py", "app/services/upload_service.py", "tests/test_graph_publication_draft.py",
            "tests/test_graph_publication_intent.py")},
        "limitations": [
            "Claim processes and a SIGKILL extraction-stage process use mocked AI, not full API/ingestion-worker operation.",
            "Killed-process recovery uses an explicit expired-lease fixture, not a natural lease-duration wait.",
            "Source/vector/graph counts and extracted graph output are authored fixtures; no Qdrant, Neo4j or R2 consistency claim.",
            "Scheduler/graph-only worker/recovery projection are not connected; no production automatic continuation is enabled.",
            "An immutable intent now survives document/job deletion, but no remote write, receipt, projection-marker lookup or reconciler consumes it. Draft admission concurrency uses two sessions in one process, not full independent workers; limits/cleanup are default-disabled/unscheduled and logical, not physical storage measurements.",
            "Optional logical snapshot budgets and terminal-body cleanup are tested but default-disabled/unscheduled; JSON text bytes are not physical disk, WAL or memory measurements.",
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
