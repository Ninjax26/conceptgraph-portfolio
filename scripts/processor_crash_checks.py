"""Actual coordinator subprocesses killed while a complete Qdrant PUT is held."""
import argparse
import asyncio
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import signal
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
import pymupdf
from qdrant_client import models
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core import processing_coordinator as coordination
from app.models.document_upload import Course, DocumentUpload, ProcessingAttempt, VectorDeletionTombstone
from app.schemas.extraction import ConceptNode, ConceptRelationship, GraphExtractionResponse
from app.services import document_processing_service as processing
from app.services import ingestion_service as ingestion_module
from app.services import rag_service as retrieval_module
from app.services.course_service import CourseNotFoundError, CourseNotReadyError, CourseService
from app.services.parser_service import ParserService
from app.services.storage_service import StorageService
from app.services.upload_service import UploadService
from scripts.deferred_vector_proxy import DeferredVectorProxy
from scripts.evaluate_graph_projection import rows
from scripts.evaluate_provider_admission import engine_for


class AuthoredIngestion(ingestion_module.IngestionService):
    def __init__(self, driver, vector, collection):
        super().__init__(graph_driver=driver, vector_client=vector)
        self.collection_name = collection
        self._embedding_model = SimpleNamespace(encode=lambda texts, **kw:
            np.array([[1., 0., 0.] for _ in texts]))

    async def extract_graph_from_chunks(self, chunks, **kwargs):
        chunk = chunks[0]
        return GraphExtractionResponse(nodes=[ConceptNode(id=key, name=name, type="concept",
            upload_id=chunk.metadata["upload_id"], document_name=chunk.metadata["document_name"],
            source_chunk_id=chunk.id, page_number=chunk.metadata["page_number"])
            for key, name in (("dns", "DNS Resolution"), ("ip", "IP Address"))],
            relationships=[ConceptRelationship(source_node_id="dns", target_node_id="ip", relation_type="RELATED_TO")],
            sections_total=1, sections_processed=1, sections_succeeded=1, batches_total=1, batches_succeeded=1)


def fixture_config(storage):
    return storage.config.model_copy(update={"embedding_provider": "local", "embedding_dimension": 3,
        "processing_role": "worker", "processing_concurrency": 1, "processing_queue_capacity": 2,
        "processing_dispatch_interval_seconds": .25})


async def child(args):
    from scripts.evaluate_upload_deletion import clients
    context = json.loads(args.context.read_text())
    engine = engine_for(SimpleNamespace(socket_dir=Path(context["socket_dir"]), port=context["port"]), context["schema"])
    driver, vector, storage = clients(context)
    sessions = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    cfg = fixture_config(storage)
    assert cfg.graph_projection_fencing_enabled
    ingestion = AuthoredIngestion(driver, vector, context["collection"])
    processor = processing.DocumentProcessingService(config=cfg, ingestion_service=ingestion,
        parser_service=ParserService(ocr_enabled=False))
    coordinator = coordination.ProcessingCoordinator(config=cfg, processing_service=processor)
    try:
        with ExitStack() as stack:
            stack.enter_context(patch.object(processing, "AsyncSessionLocal", sessions))
            stack.enter_context(patch.object(coordination, "AsyncSessionLocal", sessions))
            stack.enter_context(patch.object(processing, "storage_service", storage))
            stack.enter_context(patch.object(processing, "settings", cfg))
            stack.enter_context(patch.object(ingestion_module, "settings", cfg))
            await coordinator.start()
            try:
                async with asyncio.timeout(35):
                    while True:
                        async with sessions() as session:
                            record = await session.get(DocumentUpload, args.upload_id)
                            if record is None or record.status == "failed":
                                raise AssertionError("Fixture worker did not reach READY")
                            if record.status == "ready":
                                print(json.dumps({"status": "ready", "attempt_count": record.attempt_count}), flush=True)
                                break
                        await asyncio.sleep(.05)
            finally:
                await coordinator.stop()
    finally:
        vector.close()
        await driver.close()
        await engine.dispose()


async def verify_worker_crashes(context, sessions, driver, vector, storage, remove):
    checks, observations = {}, {}
    cfg, uploads = fixture_config(storage), UploadService()
    assert cfg.graph_projection_fencing_enabled
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_textbox((60, 70, 530, 500),
            "DNS Resolution\nDNS translates a domain name into an IP address before a browser sends packets. "
            "PMP-104 requires monthly inspection in this synthetic fixture. This native PDF is used only "
            "for worker interruption and evidence provenance checks. TLS protects the connection and "
            "a digital certificate identifies the server. None of this fixture measures AI quality.", fontsize=12)
        content = pdf.tobytes()

    async def seed():
        course = Course(id=str(uuid4()), normalized_name="worker "+uuid4().hex, display_name="Worker crash fixture")
        upload, task = str(uuid4()), str(uuid4())
        digest = hashlib.sha256(content).hexdigest()
        key = StorageService.object_key(course.id, digest)
        await asyncio.to_thread(storage.put_pdf, key, content)
        async with sessions() as session:
            session.add(course)
            await session.commit()
            await uploads.create_upload(session, upload_id=upload, task_id=task, course=course,
                content_hash=digest, original_filename="worker-crash-fixture.pdf", storage_key=key,
                page_count=1, source_bytes=len(content), reserved_chunk_count=1)
        return SimpleNamespace(upload=upload, task=task, course=course, key=key)

    async def count(item, task=None):
        conditions = [models.FieldCondition(key="upload_id", match=models.MatchValue(value=item.upload))]
        if task is not None:
            conditions.append(models.FieldCondition(key="execution_token", match=models.MatchValue(value=task)))
        return await asyncio.to_thread(lambda: vector.count(context["collection"],
            count_filter=models.Filter(must=conditions), exact=True).count)

    async def state(item):
        async with sessions() as session:
            record = await session.get(DocumentUpload, item.upload)
            try:
                ready = await CourseService().get_ready_context(session, item.course.id)
                eligible = item.upload in ready.document_ids
            except (CourseNotFoundError, CourseNotReadyError):
                eligible = False
        return {"metadata": record is not None, "eligible": eligible, "vectors": await count(item),
            "concepts": (await rows(driver, "MATCH(n:Concept {upload_id:$id}) RETURN count(n) AS n", id=item.upload))[0]["n"],
            "source": await asyncio.to_thread(storage.exists, item.key)}

    async def launch(manifest, item):
        return await asyncio.create_subprocess_exec(sys.executable, str(Path(__file__).resolve()),
            "--context", str(manifest), "--upload-id", item.upload,
            cwd="/private/tmp", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)

    async def finish(process, *, killed=False):
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), 40)
        except BaseException:
            if process.returncode is None:
                process.kill()
            await process.communicate()
            raise
        expected = -signal.SIGKILL if killed else 0
        if process.returncode != expected:
            raise RuntimeError(f"Fixture worker exited {process.returncode}; expected {expected}: {stderr.decode()[-1600:]}")
        return None if killed else json.loads(stdout.decode().splitlines()[-1])

    async def capture_request(process, proxy):
        exited = asyncio.create_task(process.wait())
        try:
            done, _ = await asyncio.wait({exited, proxy.captured}, timeout=20,
                return_when=asyncio.FIRST_COMPLETED)
            if proxy.captured in done:
                return proxy.captured.result()
            if exited in done:
                try:
                    await finish(process)
                except Exception as error:
                    raise RuntimeError(f"Private proxy rejections: {proxy.rejections}") from error
                raise AssertionError("Fixture worker finished without the delayed PUT")
            raise TimeoutError("Fixture worker did not issue the expected vector PUT")
        finally:
            exited.cancel()
            await asyncio.gather(exited, return_exceptions=True)

    with tempfile.TemporaryDirectory(prefix="cg-worker-crash-", dir="/private/tmp") as directory:
        manifest = Path(directory)/"context.json"
        for scenario in ("after_restart", "after_completed_delete"):
            item = await seed()
            process = None
            async with DeferredVectorProxy(context["qdrant"], context["collection"], item.upload, item.task) as proxy:
                manifest.write_text(json.dumps({**context, "qdrant": proxy.url}))
                manifest.chmod(0o600)
                try:
                    process = await launch(manifest, item)
                    captured = await capture_request(process, proxy)
                    async with sessions() as session:
                        record = await session.get(DocumentUpload, item.upload)
                        assert record.stage == "EMBEDDING" and record.attempt_count == 1
                        locked = (await session.execute(select(DocumentUpload.upload_id).where(
                            DocumentUpload.upload_id == item.upload).with_for_update(skip_locked=True))).all()
                        assert locked == [], "The blocked upsert did not retain its source row lock"
                    assert await count(item) == 0
                    process.kill()
                    await finish(process, killed=True)
                    async with sessions() as session:
                        released = (await session.execute(select(DocumentUpload).where(
                            DocumentUpload.upload_id == item.upload).with_for_update())).scalar_one()
                        assert released.task_id == item.task and released.status == "active"
                        # Advance only this owned fixture's lease; let the real coordinator recover it.
                        released.lease_expires_at = (await session.execute(text("SELECT now() - interval '1 second'"))).scalar_one()
                        await session.commit()
                    checks[f"sigkill_{scenario}_releases_source_lock_after_accepted_put"] = True

                    manifest.write_text(json.dumps(context))
                    replacement = await launch(manifest, item)
                    recovered = await finish(replacement)
                    assert recovered == {"status": "ready", "attempt_count": 2}
                    async with sessions() as session:
                        record = await session.get(DocumentUpload, item.upload)
                        new_task = record.task_id
                        attempts = (await session.execute(select(ProcessingAttempt).where(
                            ProcessingAttempt.document_id == item.upload).order_by(ProcessingAttempt.attempt_number))).scalars().all()
                        assert new_task != item.task and len(attempts) == 2
                        assert attempts[0].stage == "FAILED" and attempts[0].failure_category == "WORKER_ERROR"
                        ready = await CourseService().get_ready_context(session, item.course.id)
                    current_count = await count(item, new_task)
                    assert current_count > 0
                    checks[f"fresh_coordinator_{scenario}_recovers_second_attempt_to_ready"] = True
                    if scenario == "after_restart":
                        delivery = await proxy.release()
                        assert delivery["completed"] and delivery["body_sha256"] == captured["body_sha256"]
                        assert await count(item, new_task) == current_count > 0
                        assert await count(item, item.task) == captured["point_count"]
                        retrieval = retrieval_module.RetrievalService(driver, vector)
                        retrieval.collection_name = context["collection"]
                        retrieval._embedding_model = SimpleNamespace(encode=lambda *a, **kw: np.array([1., 0., 0.]))
                        with patch.object(retrieval_module, "settings", cfg):
                            evidence = await retrieval.retrieve("Inspect PMP-104", ready, top_k=1, retrieval_mode="vector_only")
                        assert len(evidence["chunks"]) == 1
                        assert evidence["chunks"][0]["metadata"]["execution_token"] == new_task
                        checks["fresh_coordinator_recovers_new_attempt_and_excludes_post_sigkill_old_put"] = True
                        observations[scenario] = {"worker_exit_code": process.returncode,
                            "captured_points": captured["point_count"], "delivery": delivery,
                            "attempt_count": 2, "current_vectors_preserved": current_count,
                            "old_vectors_excluded_from_ready_search": True}
                        assert (await remove(item.upload)).status_code == 204
                        assert await state(item) == {"metadata": False, "eligible": False, "vectors": 0, "concepts": 0, "source": False}
                        checks["deletion_after_delayed_put_removes_both_attempts"] = True
                    else:
                        assert (await remove(item.upload)).status_code == 204
                        before = await state(item)
                        assert before == {"metadata": False, "eligible": False, "vectors": 0, "concepts": 0, "source": False}
                        async with sessions() as session:
                            tracking = (await session.execute(select(VectorDeletionTombstone).where(
                                VectorDeletionTombstone.upload_id == item.upload))).scalars().all()
                            assert {row.task_id for row in tracking} == {item.task, new_task}
                        from scripts.evaluate_upload_deletion import ROOT as fixture_root
                        async def sweep():
                            cleanup = await asyncio.create_subprocess_exec(sys.executable,
                                str(fixture_root/"scripts/evaluate_upload_deletion.py"), "--recover-child", "--context", str(manifest),
                                cwd="/private/tmp", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                            return await finish(cleanup)
                        async def due_again():
                            from sqlalchemy import update
                            async with sessions() as session:
                                await session.execute(update(VectorDeletionTombstone).where(
                                    VectorDeletionTombstone.upload_id == item.upload).values(
                                    next_attempt_at=text("now() - interval '1 second'")))
                                await session.commit()
                        empty_sweep = await sweep()
                        async with sessions() as session:
                            tracking = (await session.execute(select(VectorDeletionTombstone).where(
                                VectorDeletionTombstone.upload_id == item.upload))).scalars().all()
                            assert len(tracking) == 2 and all(row.last_checked_at is not None for row in tracking)
                        delivery = await proxy.release()
                        assert delivery["completed"] and delivery["body_sha256"] == captured["body_sha256"]
                        after = await state(item)
                        assert after["vectors"] == 1
                        saved_points, _ = await asyncio.to_thread(vector.scroll, context["collection"],
                            scroll_filter=models.Filter(must=[models.FieldCondition(key="upload_id", match=models.MatchValue(value=item.upload))]),
                            with_vectors=True, with_payload=True)
                        cooldown = await sweep()
                        assert cooldown["vectors_checked"] == 0 and await count(item) == 1
                        await due_again()
                        recovered_sweep = await sweep()
                        after_sweep = await state(item)
                        assert after_sweep == before
                        # Explicit point replay tests another arrival after an acknowledged
                        # cleanup. It is not presented as a second killed-network request.
                        await asyncio.to_thread(vector.upsert, context["collection"], points=[models.PointStruct(
                            id=point.id, vector=point.vector, payload=point.payload) for point in saved_points], wait=True)
                        assert await count(item) == 1
                        await due_again()
                        repeated_sweep = await sweep()
                        repeated_state = await state(item)
                        assert repeated_state == before
                        observations[scenario] = {"worker_exit_code": process.returncode, "attempt_count": 2,
                            "before_delivery": before,
                            "empty_runtime_sweep_before_delivery": empty_sweep,
                            "delivery": delivery, "after_delivery": after, "cooldown_sweep": cooldown,
                            "fresh_runtime_sweep": recovered_sweep, "after_runtime_sweep": after_sweep,
                            "manual_point_replay_after_successful_cleanup": True,
                            "repeated_runtime_sweep": repeated_sweep, "after_repeated_runtime_sweep": repeated_state,
                            "activation_safe": repeated_state == before,
                            "manual_fixture_vector_cleanup_confirmed": False}
                        checks["deleted_source_stays_ineligible_after_post_sigkill_late_put"] = not after_sweep["eligible"]
                        checks["tracking_survives_empty_sweep_and_fresh_process_removes_late_vector"] = True
                        checks["tracking_survives_success_and_removes_subsequent_point_replay"] = True
                        checks["retry_cooldown_prevents_immediate_repeat_cleanup"] = True
                finally:
                    if process is not None and process.returncode is None:
                        process.kill()
                        await process.communicate()
    return {"checks": checks, "observations": observations,
        "activation_safe": observations["after_completed_delete"]["activation_safe"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--context", type=Path, required=True)
    parser.add_argument("--upload-id", required=True)
    asyncio.run(child(parser.parse_args()))
