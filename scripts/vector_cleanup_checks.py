"""Private-store durability, isolation and cancellation checks for cleanup."""
import asyncio
from datetime import datetime, timezone
import threading
from unittest.mock import patch
from uuid import uuid4

from qdrant_client import models
from sqlalchemy import delete, func, select, update

from app.core import database
from app.models.document_upload import Course, DocumentUpload, ProcessingAttempt, VectorDeletionTombstone
from app.services.ingestion_service import IngestionService
from app.services.resource_budget_service import resource_budget_service
from app.services.upload_service import UploadService
from app.services.vector_cleanup_service import VectorCleanupScope, VectorCleanupService


async def verify_vector_cleanup(engine, sessions, driver, vector, storage, collection):
    checks = {}
    scope = VectorCleanupScope.from_url(storage.config.qdrant_url, collection)
    ingestion = IngestionService(driver, vector)
    ingestion.collection_name = collection
    upload, old, current, neighbor = (str(uuid4()) for _ in range(4))
    course = Course(id=str(uuid4()), normalized_name="vector cleanup "+uuid4().hex, display_name="Cleanup fixture")
    point_ids = [str(uuid4()) for _ in range(4)]
    points = [models.PointStruct(id=point_id, vector=[1., 0., 0.],
        payload={"upload_id": source, "execution_token": task}) for point_id, (source, task) in zip(point_ids,
        ((upload, old), (upload, current), (neighbor, old), (upload, "foreign-task")), strict=True)]

    async def counts():
        records = await asyncio.to_thread(vector.retrieve, collection, point_ids, with_payload=True)
        return {str(row.id) for row in records}

    async def due():
        async with sessions() as session:
            await session.execute(update(VectorDeletionTombstone).where(
                VectorDeletionTombstone.upload_id == upload).values(next_attempt_at=func.now()))
            await session.commit()

    async def retained():
        async with sessions() as session:
            return list((await session.execute(select(VectorDeletionTombstone).where(
                VectorDeletionTombstone.upload_id == upload))).scalars().all())

    try:
        async with sessions() as session:
            session.add(course)
            await session.flush()
            session.add(DocumentUpload(upload_id=upload, task_id=current, course_id=course.display_name,
                course_uuid=course.id, content_hash="a"*64, week_number=1, original_filename="never-stored-fixture.pdf",
                storage_key="never-stored-fixture", status="ready", stage="READY", attempt_count=2,
                processed_chunk_count=1, graph_status="READY_WITHOUT_GRAPH", result_json={}))
            await session.flush()
            for number, task in enumerate((old, current), 1):
                session.add(ProcessingAttempt(id=str(uuid4()), document_id=upload, task_id=task,
                    attempt_number=number, stage="FAILED" if number == 1 else "READY"))
            await session.commit()
        # Exercise reversible additive initialization before any obligations exist.
        async with engine.begin() as connection:
            await connection.run_sync(lambda sync: VectorDeletionTombstone.__table__.drop(sync))
        with patch.object(database, "postgres_engine", engine):
            await database.initialize_database_schema()
            await database.initialize_database_schema()
        async with sessions() as session:
            record = await session.get(DocumentUpload, upload)
            assert record.stage == "READY" and record.task_id == current and record.attempt_count == 2
            assert len((await session.execute(select(ProcessingAttempt).where(
                ProcessingAttempt.document_id == upload))).scalars().all()) == 2
        checks["additive_schema_reversal_and_reinitialization_preserve_existing_source_and_attempts"] = True

        async with sessions() as session:
            await resource_budget_service.lock(session)
            record = await session.get(DocumentUpload, upload, with_for_update=True)
            record.result_json = {"deletion_requested": True}
            await VectorCleanupService.stage(session, record, scope)
            await session.rollback()
        assert await retained() == []
        async with sessions() as session:
            assert (await session.get(DocumentUpload, upload)).result_json == {}
        checks["staging_and_removal_flag_roll_back_together_before_commit"] = True

        async with sessions() as session:
            assert await UploadService().lock_for_deletion(session, upload, vector_cleanup_scope=scope)
            await session.rollback()
        async with sessions() as session:
            assert await UploadService().lock_for_deletion(session, upload, vector_cleanup_scope=scope)
            await session.rollback()
        assert {row.task_id for row in await retained()} == {old, current}
        checks["committed_tracking_is_idempotent_and_includes_all_source_attempts"] = True
        await asyncio.to_thread(vector.upsert, collection, points=points, wait=True)
        assert not await VectorCleanupService.reconcile(sessions, upload, old, scope, ingestion)
        async with sessions() as session:
            assert await VectorCleanupService.due(session, scope, limit=1) == []
            await resource_budget_service.lock(session)
            await session.delete(await session.get(DocumentUpload, upload))
            await session.commit()
        assert len(await retained()) == 2
        checks["live_source_blocks_cleanup_and_tracking_survives_source_cascade"] = True
        await due()
        assert await VectorCleanupService.reconcile(sessions, upload, old, scope, ingestion)
        assert await counts() == set(point_ids[1:])
        checks["remote_cleanup_filters_both_upload_and_execution_preserving_neighbor_and_other_tasks"] = True
        other_scope = VectorCleanupScope.from_url("http://127.0.0.1:1", collection)
        assert not await VectorCleanupService.reconcile(sessions, upload, current, other_scope, ingestion)
        assert await counts() == set(point_ids[1:])
        checks["store_binding_mismatch_cannot_redirect_cleanup"] = True

        original = ingestion.cleanup_vectors
        def lost_ack(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("synthetic acknowledgement lost after committed vector deletion")
        with patch.object(ingestion, "cleanup_vectors", lost_ack):
            assert not await VectorCleanupService.reconcile(sessions, upload, current, scope, ingestion)
        rows = {row.task_id: row for row in await retained()}
        assert rows[current].last_error_type == "RuntimeError"
        assert point_ids[1] not in await counts()
        await asyncio.to_thread(vector.upsert, collection, points=[points[1]], wait=True)
        await due()
        assert await VectorCleanupService.reconcile(sessions, upload, current, scope, ingestion)
        assert point_ids[1] not in await counts()
        checks["ambiguous_delete_ack_retains_obligation_and_replay_is_cleaned_on_retry"] = True

        # Recreate only an owned synthetic source, then prove fail-closed behavior.
        async with sessions() as session:
            await resource_budget_service.lock(session)
            session.add(DocumentUpload(upload_id=upload, task_id=str(uuid4()), course_id=course.display_name,
                course_uuid=course.id, content_hash="a"*64, week_number=1, original_filename="never-stored-fixture.pdf",
                storage_key="never-stored-fixture", status="active", stage="UPLOADED"))
            await session.commit()
        await due()
        await asyncio.to_thread(vector.upsert, collection, points=[points[0]], wait=True)
        assert not await VectorCleanupService.reconcile(sessions, upload, old, scope, ingestion)
        assert point_ids[0] in await counts()
        async with sessions() as session:
            await resource_budget_service.lock(session)
            await session.delete(await session.get(DocumentUpload, upload))
            await session.commit()
        checks["recreated_source_blocks_retained_old_cleanup"] = True

        await due()
        entered, release = threading.Event(), threading.Event()
        def held(*args, **kwargs):
            entered.set()
            if not release.wait(10):
                raise RuntimeError("private cleanup release timed out")
            original(*args, **kwargs)
        worker = None
        with patch.object(ingestion, "cleanup_vectors", held):
            try:
                worker = asyncio.create_task(VectorCleanupService.reconcile(sessions, upload, old, scope, ingestion))
                assert await asyncio.to_thread(entered.wait, 5)
                assert not await VectorCleanupService.reconcile(sessions, upload, old, scope, ingestion)
                worker.cancel()
                await asyncio.sleep(0)
                worker.cancel()
                await asyncio.sleep(.05)
                assert not worker.done()
                async with sessions() as session:
                    assert not await resource_budget_service.try_lock(session)
                release.set()
                try:
                    await worker
                except asyncio.CancelledError:
                    pass
                else:
                    raise AssertionError("Cleanup swallowed cancellation")
            finally:
                release.set()
                if worker is not None:
                    await asyncio.gather(worker, return_exceptions=True)
        async with sessions() as session:
            assert await resource_budget_service.try_lock(session)
        assert point_ids[0] not in await counts()
        assert len(await retained()) == 2
        checks["concurrent_reconciler_yields_to_existing_claim"] = True
        checks["repeated_cancellation_drains_remote_delete_before_releasing_admission"] = True
        checks["acknowledged_cleanup_keeps_tracking_for_future_late_writes"] = True
        await due()
        async with sessions() as session:
            assert len(await VectorCleanupService.due(session, scope, limit=1)) == 1
        checks["database_due_discovery_respects_explicit_batch_bound"] = True
    finally:
        await asyncio.to_thread(vector.delete, collection, points_selector=models.PointIdsList(points=point_ids), wait=True)
        async with sessions() as session:
            await session.execute(delete(DocumentUpload).where(DocumentUpload.upload_id == upload))
            await session.execute(delete(VectorDeletionTombstone).where(VectorDeletionTombstone.upload_id == upload))
            await session.execute(delete(Course).where(Course.id == course.id))
            await session.commit()
    return checks
