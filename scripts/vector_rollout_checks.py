"""Structural rollout-audit qualification inside the owned four-store fixture."""
from dataclasses import replace
import hashlib
import json
from uuid import uuid4

from qdrant_client import models
from sqlalchemy import delete, select

from app.models.document_upload import Course, DocumentUpload, ProcessingAttempt
from app.services.ingestion_service import IngestionService
from app.services.vector_rollout_audit import VectorReadContract, ReadyVectorIdentity, audit_ready_vectors
from scripts.evaluate_graph_retry import fixture


async def verify_vector_rollout(sessions, vector, config, collection):
    cfg = config.model_copy(update={"embedding_provider":"local", "embedding_dimension":3})
    contract = VectorReadContract.from_settings(cfg)
    course = Course(id=str(uuid4()), normalized_name="rollout-"+uuid4().hex, display_name="Synthetic audit")
    async with sessions() as session:
        session.add(course)
        await session.commit()
    upload, old_task, _ = await fixture(sessions, course, delay=None)
    current_task = str(uuid4())
    async with sessions() as session:
        record = await session.get(DocumentUpload, upload)
        record.task_id, record.attempt_count = current_task, 2
        attempt = (await session.execute(select(ProcessingAttempt).where(
            ProcessingAttempt.document_id == upload))).scalar_one()
        attempt.stage = "FAILED"
        session.add(ProcessingAttempt(id=str(uuid4()), document_id=upload, task_id=current_task,
            attempt_number=2, stage="READY"))
        await session.commit()
    async with sessions() as session:
        record = await session.get(DocumentUpload, upload)
        tasks = (await session.execute(select(ProcessingAttempt.task_id).where(
            ProcessingAttempt.document_id == upload))).scalars().all()
        identity = ReadyVectorIdentity.from_record(record, known_tasks=tasks)

    checks, observed = {}, {}
    calls, keys = [], []
    class ReadOnlyClient:
        def get_collection(self, name):
            calls.append("get_collection")
            return vector.get_collection(name)
        def scroll(self, **kwargs):
            calls.append("scroll")
            result = vector.scroll(**kwargs)
            keys.extend(set(row.payload) for row in result[0])
            assert kwargs["with_vectors"] is False
            return result

    def point(chunk, *, task=current_task, course_id=course.id, physical=None):
        return models.PointStruct(id=physical or IngestionService._qdrant_point_id(chunk), vector=[1.,0.,0.],
            payload={"upload_id":upload,"document_id":course_id,"execution_token":task,"chunk_id":chunk,
                "page_number":1,"text":"Synthetic private-looking text excluded from audit projection"})
    baseline = [point(f"{upload}:1:{i}", physical=(IngestionService._qdrant_point_id(
        f"{upload}:1:{i}", execution=(upload,current_task)) if i == 1 else None)) for i in range(3)]
    own_filter = models.Filter(must=[models.FieldCondition(key="upload_id", match=models.MatchValue(value=upload))])
    neighbor = models.PointStruct(id=str(uuid4()), vector=[1.,0.,0.],
        payload={"upload_id":str(uuid4()),"text":"Unrelated synthetic neighbor"})
    vector.upsert(collection, points=[neighbor], wait=True)

    async def snapshot():
        points, cursor = vector.scroll(collection, limit=256, with_payload=True, with_vectors=True)
        assert cursor is None
        async with sessions() as session:
            row = await session.get(DocumentUpload, upload)
            metadata = (row.task_id, row.stage, row.processed_chunk_count, row.result_json)
            history = (await session.execute(select(ProcessingAttempt.task_id, ProcessingAttempt.stage).where(
                ProcessingAttempt.document_id == upload).order_by(ProcessingAttempt.attempt_number))).all()
        vector_hash = hashlib.sha256(json.dumps([p.model_dump(mode="json") for p in points],
            sort_keys=True).encode()).hexdigest()
        return vector_hash, metadata, history

    async def audit(points, **kwargs):
        vector.delete(collection, points_selector=own_filter, wait=True)
        vector.upsert(collection, points=points, wait=True)
        before = await snapshot()
        result = await audit_ready_vectors(ReadOnlyClient(), contract, identity,
            peer_contract=contract, page_size=1, **kwargs)
        assert await snapshot() == before
        assert result["activation_allowed"] is result["writer_drain_verified"] is False
        return result

    try:
        good = await audit(baseline)
        assert good["read_coverage_ok"] and good["cleanup_payloads_trackable"]
        assert good["counts"]["current_unique_chunks"] == 3
        checks["paginated_current_payloads_accept_legacy_and_scoped_physical_ids"] = True
        stale = await audit([*baseline, point("stale", task=old_task)])
        assert stale["read_coverage_ok"] and stale["cleanup_payloads_trackable"]
        assert stale["counts"]["known_stale_points"] == 1
        checks["postgres_attempt_history_distinguishes_known_stale_execution"] = True
        for label, task, counter in (("unknown", str(uuid4()), "unknown_task_points"),
                                     ("untagged", None, "untagged_points")):
            result = await audit([*baseline, point(label, task=task)])
            assert result["read_coverage_ok"] and not result["cleanup_payloads_trackable"]
            assert result["counts"][counter] == 1
            checks[label+"_payloads_do_not_certify_cleanup_coverage"] = True
        assert not (await audit(baseline[:2]))["read_coverage_ok"]
        checks["missing_current_chunks_fail_even_when_some_vectors_exist"] = True
        assert not (await audit([*baseline, point(baseline[0].payload["chunk_id"], physical=str(uuid4()))]))["read_coverage_ok"]
        checks["duplicate_logical_chunk_is_not_additional_current_coverage"] = True
        assert not (await audit([*baseline, point("cross-course", course_id=str(uuid4()))]))["read_coverage_ok"]
        checks["wrong_course_payloads_fail_closed"] = True
        partial = await audit(baseline, max_points=2)
        assert not partial["scan_completed"] and not partial["read_coverage_ok"]
        checks["bounded_partial_scan_is_not_complete_coverage"] = True
        before_calls = len(calls)
        drift = await audit_ready_vectors(ReadOnlyClient(), contract, identity,
            peer_contract=replace(contract, dimension=4))
        assert not drift["scan_completed"] and len(calls) == before_calls
        checks["peer_vector_contract_drift_stops_before_remote_reads"] = True
        assert keys and all("text" not in fields for fields in keys)
        checks["only_metadata_is_read_and_all_audits_preserve_postgres_vectors_and_neighbor"] = True
        observed = {"compatible":good,"known_stale":stale,"partial":partial,
            "read_only_client_calls":len(calls),"model_calls":0,"production_data_mutations":0,
            "postgres_neo4j_r2_fleet_compatibility_verified":False,"writer_drain_verified":False}
    finally:
        vector.delete(collection, points_selector=own_filter, wait=True)
        vector.delete(collection, points_selector=models.PointIdsList(points=[neighbor.id]), wait=True)
        async with sessions() as session:
            await session.execute(delete(DocumentUpload).where(DocumentUpload.upload_id == upload))
            await session.execute(delete(Course).where(Course.id == course.id))
            await session.commit()
    return {"checks":checks,"observations":observed}
