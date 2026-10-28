"""Actual processor with isolated stores, native PDF and authored AI outputs."""

import asyncio
from contextlib import ExitStack
import hashlib
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import numpy as np
import pymupdf
from qdrant_client import models
from sqlalchemy import select

from app.models.document_upload import Course, DocumentUpload, GraphPublicationIntent
from app.schemas.extraction import ConceptNode, ConceptRelationship, GraphExtractionResponse
from app.services import document_processing_service as processing
from app.services import ingestion_service as ingestion_module
from app.services.course_service import CourseService
from app.services.ingestion_service import GraphProjectionMarker, IngestionService
from app.services.parser_service import ParserService
from app.services.storage_service import StorageService
from app.services.upload_service import UploadService
from scripts.evaluate_graph_projection import rows


async def verify_processor(sessions, driver, vector, storage, collection, remove):
    cfg = storage.config.model_copy(update={"embedding_provider": "local", "embedding_dimension": 3})
    assert cfg.graph_projection_fencing_enabled
    uploads, checks = UploadService(), {}
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_text((60, 70), "DNS Resolution", fontsize=18)
        page.insert_textbox((60, 110, 530, 400),
            "DNS translates a domain name into an IP address. A browser needs the address "
            "before sending packets to a server. TCP provides a connection between endpoints. "
            "TLS protects the connection. HTTPS carries an HTTP request over TLS. "
            "A certificate identifies the server. This synthetic networking lesson is used "
            "only to verify document processing and source provenance.", fontsize=12)
        content = pdf.tobytes()

    class AuthoredGraph(IngestionService):
        def __init__(self):
            super().__init__(graph_driver=driver, vector_client=vector)
            self.collection_name = collection
            self._embedding_model = SimpleNamespace(encode=lambda texts, **kw:
                np.array([[1., 0., 0.] for _ in texts]))
            self.connected = self.fail_upsert = False
            self.last_graph = None

        def upsert_chunks_to_qdrant(self, chunks):
            count = super().upsert_chunks_to_qdrant(chunks)
            if self.fail_upsert:
                vector.upsert(collection, points=[models.PointStruct(id=str(uuid4()), vector=[0., 1., 0.],
                    payload={"upload_id": chunks[0].metadata["upload_id"], "execution_token": "foreign-task"})], wait=True)
                raise RuntimeError("Qdrant synthetic lost acknowledgement after commit")
            return count

        async def extract_graph_from_chunks(self, chunks, **kwargs):
            chunk = chunks[0]
            self.last_graph = GraphExtractionResponse(nodes=[ConceptNode(id=key, name=name, type="concept",
                upload_id=chunk.metadata["upload_id"], document_name=chunk.metadata["document_name"],
                source_chunk_id=chunk.id, page_number=chunk.metadata["page_number"])
                for key, name in (("dns", "DNS Resolution"), ("ip", "IP Address"))],
                relationships=[ConceptRelationship(source_node_id="dns", target_node_id="ip", relation_type="RELATED_TO")]
                if self.connected else [], sections_total=1, sections_processed=1, sections_succeeded=1,
                batches_total=1, batches_succeeded=1)
            return self.last_graph

    service = AuthoredGraph()
    processor = processing.DocumentProcessingService(config=cfg, ingestion_service=service,
        parser_service=ParserService(ocr_enabled=False), upload_service=uploads)

    async def create():
        course = Course(id=str(uuid4()), normalized_name="processor "+uuid4().hex, display_name="Processor fixture")
        upload, task = str(uuid4()), str(uuid4())
        digest = hashlib.sha256(content).hexdigest()
        key = StorageService.object_key(course.id, digest)
        await asyncio.to_thread(storage.put_pdf, key, content)
        async with sessions() as session:
            session.add(course)
            await session.commit()
            await uploads.create_upload(session, upload_id=upload, task_id=task, course=course,
                content_hash=digest, original_filename="processor-fixture.pdf", storage_key=key,
                page_count=1, source_bytes=len(content), reserved_chunk_count=1)
            assert await uploads.claim_for_processing(session, upload_id=upload, task_id=task,
                lease_owner="processor-fixture", lease_seconds=180)
        return SimpleNamespace(upload=upload, task=task, course=course, key=key)

    async def run(item):
        return await processor.process_document(item.upload, item.task, lease_owner="processor-fixture")

    async def vectors(item, token=None):
        conditions = [models.FieldCondition(key="upload_id", match=models.MatchValue(value=item.upload))]
        if token is not None:
            conditions.append(models.FieldCondition(key="execution_token", match=models.MatchValue(value=token)))
        return await asyncio.to_thread(lambda: vector.count(collection,
            count_filter=models.Filter(must=conditions), exact=True).count)

    with ExitStack() as stack:
        stack.enter_context(patch.object(processing, "AsyncSessionLocal", sessions))
        stack.enter_context(patch.object(processing, "storage_service", storage))
        stack.enter_context(patch.object(processing, "settings", cfg))
        stack.enter_context(patch.object(ingestion_module, "settings", cfg))
        item = await create()
        result = await run(item)
        assert result["status"] == "ready" and result["graph_status"] == "GRAPH_PARTIAL"
        async with sessions() as session:
            record = await session.get(DocumentUpload, item.upload)
            ready = await CourseService().get_ready_context(session, item.course.id)
            assert item.upload in ready.document_ids and record.processed_chunk_count > 0
            assert record.processed_chunk_count == await vectors(item, item.task)
        checks["native_pdf_full_processor_reaches_ready_with_real_stores_and_authored_ai"] = True

        old_graph, old_task = service.last_graph, item.task
        marker = GraphProjectionMarker(str(uuid4()), str(uuid4()), item.upload, item.course.id,
            old_task, "a"*64, 1)
        await service.replace_graph_extraction(old_graph, item.course.id, upload_id=item.upload,
            document_name="processor-fixture.pdf", course_name=item.course.display_name,
            execution_token=old_task, marker=marker)
        item.task = str(uuid4())
        async with sessions() as session:
            retried = await uploads.retry_upload(session, item.upload, item.task)
            assert retried.attempt_count == 2
            assert await uploads.claim_for_processing(session, upload_id=item.upload, task_id=item.task,
                lease_owner="processor-fixture", lease_seconds=180)
        service.connected = True
        assert (await run(item))["graph_status"] == "GRAPH_READY"
        try:
            await service.replace_graph_extraction(old_graph, item.course.id, upload_id=item.upload,
                document_name="processor-fixture.pdf", course_name=item.course.display_name,
                execution_token=old_task, marker=marker)
        except ValueError:
            pass
        else:
            raise AssertionError("Old marked write erased the processor's retry graph")
        assert (await rows(driver, "MATCH(n:Concept {upload_id:$upload,execution_token:$task}) RETURN count(n) AS n",
            upload=item.upload, task=item.task))[0]["n"] == 2
        checks["real_manual_retry_advances_graph_generation_and_rejects_older_marked_replay"] = True
        try:
            await processor._with_current_attempt(item.upload, old_task, "processor-fixture",
                lambda session, current: service.cleanup_upload(item.upload, item.course.id))
        except processing.ProcessingAttemptSuperseded:
            pass
        else:
            raise AssertionError("Stale attempt entered cleanup")
        assert await vectors(item, item.task) > 0
        checks["stale_processor_source_cannot_enter_graph_or_vector_cleanup"] = True

        failed = await create()
        service.fail_upsert = True
        assert (await run(failed))["status"] == "failed"
        service.fail_upsert = False
        async with sessions() as session:
            record = await session.get(DocumentUpload, failed.upload)
            assert record.stage == "FAILED" and record.retryable
        assert await vectors(failed, failed.task) == 0 and await vectors(failed, "foreign-task") == 1
        fence = (await rows(driver, "MATCH(f:GraphProjectionFence {upload_id:$id}) RETURN f.aborted AS aborted",
            id=failed.upload))[0]
        assert fence["aborted"] and await asyncio.to_thread(storage.exists, failed.key)
        checks["failed_processor_closes_attempt_and_removes_only_its_vector_payloads"] = True

        plain = await create()
        assert (await run(plain))["status"] == "ready"
        baseline_graph = service.last_graph
        async with sessions() as session:
            assert (await session.execute(select(GraphPublicationIntent).where(
                GraphPublicationIntent.upload_id == plain.upload))).scalar_one_or_none() is None
        assert (await remove(plain.upload)).status_code == 204
        try:
            await service.replace_graph_extraction(baseline_graph, plain.course.id, upload_id=plain.upload,
                document_name="processor-fixture.pdf", course_name=plain.course.display_name,
                execution_token=plain.task, source_generation=1)
        except ValueError:
            pass
        else:
            raise AssertionError("A baseline without a publication intent recreated deleted graph data")
        async with sessions() as session:
            assert await session.get(DocumentUpload, plain.upload) is None
        assert await vectors(plain) == 0 and not await asyncio.to_thread(storage.exists, plain.key)
        checks["authenticated_delete_tombstones_baseline_without_publication_intent"] = True

        cancelled = await create()
        entered, release, acquired = asyncio.Event(), asyncio.Event(), asyncio.Event()
        original = service._write_graph_extraction
        async def held(tx, *args, **kwargs):
            entered.set()
            await release.wait()
            await original(tx, *args, **kwargs)
        async def contender():
            async with sessions() as session:
                await session.execute(select(DocumentUpload).where(
                    DocumentUpload.upload_id == cancelled.upload).with_for_update())
                acquired.set()
        worker = observer = None
        with patch.object(service, "_write_graph_extraction", held):
            try:
                worker = asyncio.create_task(run(cancelled))
                await asyncio.wait_for(entered.wait(), 15)
                observer = asyncio.create_task(contender())
                worker.cancel()
                await asyncio.sleep(0)
                worker.cancel()
                await asyncio.sleep(0.1)
                assert not acquired.is_set() and not worker.done()
                release.set()
                try:
                    await asyncio.wait_for(worker, 15)
                except asyncio.CancelledError:
                    pass
                else:
                    raise AssertionError("Processor swallowed cancellation")
                await asyncio.wait_for(observer, 15)
            finally:
                release.set()
                await asyncio.gather(*(task for task in (worker, observer) if task is not None), return_exceptions=True)
        async with sessions() as session:
            recovered = await uploads.prepare_stale_recoveries(session)
            row = next(record for record in recovered if record.upload_id == cancelled.upload)
            assert row.attempt_count == 2 and row.task_id != cancelled.task
            cancelled.task = row.task_id
            assert await uploads.claim_for_processing(session, upload_id=cancelled.upload, task_id=cancelled.task,
                lease_owner="processor-fixture", lease_seconds=180)
        assert (await run(cancelled))["status"] == "ready"
        checks["repeated_cancellation_drains_graph_and_source_lock_then_real_recovery_uses_new_attempt"] = True

        for record in (item, failed, cancelled):
            assert (await remove(record.upload)).status_code == 204
            assert await vectors(record) == 0 and not await asyncio.to_thread(storage.exists, record.key)
        checks["processor_fixtures_remove_all_vector_and_source_artifacts_through_real_delete_route"] = True
    return checks
