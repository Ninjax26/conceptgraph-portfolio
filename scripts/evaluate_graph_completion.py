"""Isolated PostgreSQL graph completion; remote marker reads are authored doubles.

No dispatcher, Neo4j write, provider request, Qdrant or source-storage operation.
"""

import argparse
import asyncio
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core import database
from app.models.document_upload import Course, DocumentUpload, GraphContinuationJob, GraphExtractionCheckpoint, GraphPublicationDraft, GraphPublicationIntent, GraphPublicationReceipt
from app.schemas.extraction import ConceptNode, ConceptRelationship, GraphExtractionResponse
from app.services.course_service import CourseService
from app.services.graph_continuation_service import GraphContinuationService
from app.services.ingestion_service import GraphProjectionMarker
from app.services.parser_service import DocumentChunk
from scripts.evaluate_graph_continuation_jobs import configuration
from scripts.evaluate_graph_retry import fixture
from scripts.evaluate_provider_admission import engine_for


async def run(args):
    admin, engine, created = engine_for(args), None, False
    schema = "cg_admission_" + uuid4().hex
    service, checks = GraphContinuationService(), {}
    try:
        async with admin.begin() as connection:
            version = (await connection.execute(text("SHOW server_version"))).scalar_one()
            await connection.execute(text(f"CREATE SCHEMA {schema}"))
            created = True
        engine = engine_for(args, schema)
        sessions = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        with patch.object(database, "postgres_engine", engine):
            await database.initialize_database_schema()
        with ExitStack() as stack:
            configuration(stack)
            async with sessions() as session:
                course = Course(id=str(uuid4()), normalized_name="completion fixture", display_name="Completion fixture")
                session.add(course)
                await session.commit()
            upload_id, task, _ = await fixture(sessions, course, delay=1)
            async with sessions() as session:
                record = await session.get(DocumentUpload, upload_id)
                record.result_json = {**record.result_json, "graph_provider_limited": True,
                    "source_bytes": 100, "page_count": 1, "stage_seconds": {"embedding": 0.25}}
                await session.commit()
                original, original_completed = dict(record.result_json), record.completed_at
                chunks = [DocumentChunk(id=f"{upload_id}:{i}", text=f"DNS evidence {i}.", metadata={
                    "chunk_id": f"{upload_id}:{i}", "upload_id": upload_id, "document_id": course.id,
                    "execution_token": task, "document_name": record.original_filename,
                    "page_number": 1, "section_heading": "DNS"}) for i in range(3)]
                job = await service.schedule(session, upload_id=upload_id, document_task_id=task,
                    chunks=chunks, retry_after_seconds=1)
                job.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
                await session.commit()
                claim = await service.claim_next(session, lease_owner="completion-fixture", lease_seconds=180)
                assert claim is not None
                graph = GraphExtractionResponse(nodes=[ConceptNode(id=key, name=name, type="concept",
                    upload_id=upload_id, document_name=record.original_filename, page_number=1,
                    section_heading="DNS", source_chunk_id=chunks[i].id)
                    for i, (key, name) in enumerate((("dns", "DNS"), ("ip", "IP address")))],
                    relationships=[ConceptRelationship(source_node_id="dns", target_node_id="ip", relation_type="RELATED_TO")],
                    sections_total=1, sections_succeeded=1, sections_processed=1,
                    batches_total=1, batches_succeeded=1)
                limited = await service.prepare_publication(session, claim,
                    graph.model_copy(update={"provider_limited": True}))
                await service.save_publication_draft(session, claim, limited)
                limited_reader = SimpleNamespace(read_graph_publication_marker=AsyncMock())
                try:
                    await service.complete_publication(session, claim, limited_reader)
                except ValueError as exc:
                    assert "deferred" in str(exc)
                else:
                    raise AssertionError("Provider-limited output completed instead of deferring")
                assert limited_reader.read_graph_publication_marker.await_count == 0
                await session.rollback()
                await session.refresh(record)
                checks["provider_limited_draft_cannot_complete_or_read_remote_marker"] = True
                candidate = await service.prepare_publication(session, claim, graph)
                await service.save_publication_draft(session, claim, candidate)
                intent = await service.stage_publication_intent(session, claim)
                await session.commit()
                marker = GraphProjectionMarker(intent.id, claim.job_id, upload_id, course.id, task,
                    candidate.sha256, record.attempt_count)

            async def unchanged():
                async with sessions() as session:
                    record = await session.get(DocumentUpload, upload_id)
                    job = await session.get(GraphContinuationJob, claim.job_id)
                    assert record.result_json == original and record.graph_status == "GRAPH_PARTIAL"
                    assert record.task_id == task and record.stage == "READY" and job.status == "running"
                    assert (await session.execute(select(func.count()).select_from(GraphPublicationReceipt))).scalar_one() == 0

            for name, value in (("missing_marker", None), ("conflicting_marker", replace(marker, payload_sha256="b"*64))):
                reader = SimpleNamespace(read_graph_publication_marker=AsyncMock(return_value=value))
                async with sessions() as session:
                    if value is None:
                        assert not await service.complete_publication(session, claim, reader)
                    else:
                        try:
                            await service.complete_publication(session, claim, reader)
                        except ValueError:
                            pass
                        else:
                            raise AssertionError("Conflicting marker completed a job")
                    await session.rollback()
                await unchanged()
                checks[f"{name}_cannot_complete_or_change_ready_result"] = True

            for model, name in ((GraphPublicationDraft, "missing_draft"), (GraphPublicationIntent, "missing_intent")):
                reader = SimpleNamespace(read_graph_publication_marker=AsyncMock(return_value=marker))
                async with sessions() as session:
                    row = (await session.execute(select(model).where(model.job_id == claim.job_id))).scalar_one()
                    await session.delete(row)
                    await session.flush()
                    assert not await service.complete_publication(session, claim, reader)
                    assert reader.read_graph_publication_marker.await_count == 0
                    await session.rollback()
                await unchanged()
                checks[f"{name}_cannot_complete_or_read_remote_marker"] = True

            async def slow_marker(intent_id):
                assert intent_id == marker.id
                await asyncio.sleep(1.1)
                return marker
            async with sessions() as session:
                job = await session.get(GraphContinuationJob, claim.job_id)
                job.lease_expires_at = (await session.execute(select(func.clock_timestamp()))).scalar_one() + timedelta(seconds=1)
                await session.flush()
                assert not await service.complete_publication(session, claim,
                    SimpleNamespace(read_graph_publication_marker=slow_marker))
            await unchanged()
            checks["natural_lease_expiry_during_remote_read_cannot_stage_completion"] = True

            reader = SimpleNamespace(read_graph_publication_marker=AsyncMock(return_value=marker))
            async with sessions() as session:
                assert not await service.complete_publication(session, replace(claim, claim_token="stale"), reader)
                assert reader.read_graph_publication_marker.await_count == 0
            checks["stale_claim_cannot_read_marker_or_complete"] = True
            for name in ("expired_lease", "pending_removal"):
                async with sessions() as session:
                    if name == "expired_lease":
                        job = await session.get(GraphContinuationJob, claim.job_id)
                        job.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
                    else:
                        record = await session.get(DocumentUpload, upload_id)
                        record.result_json = {**original, "deletion_requested": True}
                    await session.flush()
                    assert not await service.complete_publication(session, claim, reader)
                await unchanged()
                checks[f"{name}_fences_completion"] = True

            async with sessions() as session:
                assert await service.complete_publication(session, claim, reader)
                async with sessions() as other:
                    assert (await other.execute(select(DocumentUpload.upload_id).where(
                        DocumentUpload.upload_id == upload_id).with_for_update(skip_locked=True))).scalar_one_or_none() is None
                await unchanged()  # Other sessions cannot see staged completion.
                await session.rollback()
            await unchanged()
            checks["staged_completion_keeps_source_lock_and_rolls_back_receipt_job_and_result"] = True

            async with sessions() as session:
                assert await service.complete_publication(session, claim, reader)
                await session.commit()
            with patch.object(database, "postgres_engine", engine):
                await database.initialize_database_schema()
            async with sessions() as session:
                record = await session.get(DocumentUpload, upload_id)
                job = await session.get(GraphContinuationJob, claim.job_id)
                receipt = await session.get(GraphPublicationReceipt, marker.id)
                assert job.status == "completed" and job.claim_token is None
                assert job.lease_owner is None and job.lease_expires_at is None and job.failure_category is None
                assert job.attempt_count == 1 and job.snapshot_json
                assert record.status == "ready" and record.stage == "READY" and record.task_id == task
                assert record.attempt_count == 1 and record.completed_at == original_completed
                assert record.processed_chunk_count == 3 and record.result_json["chunks_indexed"] == 3
                assert record.result_json["source_bytes"] == 100 and record.result_json["page_count"] == 1
                assert record.result_json["stage_seconds"] == {"embedding": 0.25}
                assert record.graph_node_count == 2 and record.graph_edge_count == 1
                assert record.graph_status == record.result_json["graph_status"] == "GRAPH_READY"
                assert record.result_json["graph_batches_failed"] == 0 and record.result_json["graph_batches_skipped"] == 0
                assert not record.result_json["graph_provider_limited"]
                assert "graph_retry_after_seconds" not in record.result_json and "graph_retry_not_before" not in record.result_json
                assert receipt.payload_sha256 == candidate.sha256 and receipt.source_generation == 1
                assert (await session.execute(select(func.count()).select_from(GraphExtractionCheckpoint))).scalar_one() == 1
                context = await CourseService().get_ready_context(session, course.id)
                assert context.document_ids == [upload_id] and context.documents[0].task_id == task
                assert not await service.complete_publication(session, claim, reader)
            checks["committed_completion_survives_restart_preserves_ready_identity_sources_and_checkpoints"] = True
            checks["completed_claim_cannot_complete_again"] = True
    finally:
        if engine is not None:
            await engine.dispose()
        if created:
            async with admin.begin() as connection:
                await connection.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        await admin.dispose()
    report = {"generated_at_utc": datetime.now(timezone.utc).isoformat(), "postgres_version": version,
        "checks": checks, "checks_passed": len(checks), "test_schema_removed": True,
        "hosted_provider_calls": 0, "production_test_data_mutations": 0,
        "source_sha256": {name: hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in (
            "app/services/graph_continuation_service.py", "app/services/graph_result_metadata.py",
            "app/services/document_processing_service.py", "scripts/evaluate_graph_completion.py")},
        "limitations": ["Remote marker reads are authored doubles; no Neo4j write or cross-store atomicity is proven.",
            "No API/worker dispatcher, cancellation drain, killed publisher, hosted R2 or provider use is tested.",
            "Automatic graph-only continuation remains disconnected; this is caller-owned PostgreSQL finalization only."]}
    output = ROOT/'evaluation/graph-completion-current'
    output.with_suffix('.json').write_text(json.dumps(report, indent=2)+'\n')
    output.with_suffix('.md').write_text('# Graph publication completion — isolated PostgreSQL\n\n'
        'Command: `.venv/bin/python scripts/evaluate_graph_completion.py --socket-dir <private-test-socket>`.\n\n'
        + '\n'.join(f'- {name}: passed' for name in checks) + '\n\nLimits:\n\n'
        + '\n'.join(f'- {item}' for item in report['limitations'])+'\n')
    print(json.dumps({"checks_passed": len(checks), "test_schema_removed": True, "hosted_provider_calls": 0}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--socket-dir', type=Path, required=True)
    parser.add_argument('--port', type=int, default=55432)
    asyncio.run(run(parser.parse_args()))
