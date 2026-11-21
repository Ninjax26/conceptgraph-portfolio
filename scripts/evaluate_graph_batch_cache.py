"""Run offline graph cache checks against an explicitly supplied private PostgreSQL socket."""

import argparse
import asyncio
from contextlib import ExitStack
from hashlib import sha256
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pymupdf
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core import database
from app.core.config import settings
from app.core.processing import ProcessingStage
from app.models.document_upload import Course, DocumentUpload, GraphBatchCacheRow
from app.schemas.extraction import ConceptNode, ConceptRelationship, GraphExtractionResponse
from app.services import document_processing_service as processing
from app.services.graph_batch_cache import CacheEntry, GraphBatchCache
from app.services.graph_continuation_service import GraphContinuationService
from app.services.graph_job_extraction import GraphJobExtraction
from app.services.parser_service import ParserService
from app.services.ingestion_service import IngestionService
from app.services.upload_service import UploadService
from scripts.evaluate_provider_admission import engine_for


def pdf(title):
    with pymupdf.open() as document:
        document.set_metadata({"title": title})
        document.new_page().insert_text((48, 72), "DNS maps a host name to an IP address.")
        return document.tobytes()


async def run(args):
    admin = engine_for(args)
    schema = "cg_admission_" + uuid4().hex
    checks = []
    async with admin.begin() as conn:
        await conn.execute(text(f"CREATE SCHEMA {schema}"))
    engine = engine_for(args, schema)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    uploads = UploadService()
    objects = {}
    graph = GraphExtractionResponse(nodes=[
        ConceptNode(id=name, name=name, type="concept", source_chunk_id="source-1")
        for name in ("DNS", "Host")], relationships=[
        ConceptRelationship(source_node_id="DNS", target_node_id="Host", relation_type="RELATED_TO")])

    async def new_upload(course):
        uid, task = str(uuid4()), str(uuid4())
        binary = pdf(uid)
        objects[f"fixture/{uid}"] = binary
        async with sessions() as session:
            await uploads.create_upload(session, course=course, upload_id=uid, task_id=task,
                original_filename=f"{uid}.pdf", storage_key=f"fixture/{uid}", source_bytes=len(binary),
                content_hash=sha256(binary).hexdigest(), page_count=1, reserved_chunk_count=1)
            assert await uploads.claim_for_processing(session, upload_id=uid, task_id=task,
                                                      lease_owner="fixture", lease_seconds=180)
        return uid, task

    async def rows(uid):
        async with sessions() as session:
            return list((await session.scalars(select(GraphBatchCacheRow).where(
                GraphBatchCacheRow.upload_id == uid))).all())

    async def remove(uid):
        async with sessions() as session:
            await session.execute(delete(DocumentUpload).where(DocumentUpload.upload_id == uid))
            await session.commit()

    async def process(course, *, allow_generation):
        uid, task = await new_upload(course)
        ingestion = IngestionService(None, None)
        ingestion.cleanup_vectors = Mock()
        ingestion.upsert_chunks_to_qdrant = Mock(side_effect=len)
        ingestion.cleanup_graph = AsyncMock()
        ingestion.store_graph_extraction = AsyncMock()
        ingestion.cleanup_upload = AsyncMock()
        ingestion.extract_graph_from_text = AsyncMock(return_value=graph,
            side_effect=None if allow_generation else AssertionError("Unexpected fixture generation."))
        result = await processing.DocumentProcessingService(ingestion_service=ingestion).process_document(
            uid, task, lease_owner="fixture")
        assert result["status"] == "ready" and result["graph_status"] == "GRAPH_READY", result
        assert result["graph_batch_cache_hits"] == int(not allow_generation)
        assert ingestion.extract_graph_from_text.await_count == int(allow_generation)
        projected = ingestion.store_graph_extraction.await_args.args[0]
        assert all(node.upload_id == uid and node.document_name == f"{uid}.pdf" and
                   node.source_chunk_id.startswith(uid + ":") and node.page_number == 1 for node in projected.nodes)
        assert len(await rows(uid)) == 1
        return uid, task

    try:
        with ExitStack() as stack:
            for name, value in (("graph_batch_cache_enabled", True), ("graph_global_linking_enabled", False),
                                ("embedding_provider", "local"), ("ocr_enabled", False),
                                ("graph_projection_fencing_enabled", False)):
                stack.enter_context(patch.object(settings, name, value))
            stack.enter_context(patch.object(database, "postgres_engine", engine))
            stack.enter_context(patch.object(processing, "AsyncSessionLocal", sessions))
            stack.enter_context(patch.object(processing, "storage_service",
                SimpleNamespace(get_bytes=objects.__getitem__, exists=objects.__contains__)))
            await database.initialize_database_schema()
            async with engine.begin() as conn:
                await conn.execute(text("DROP TABLE graph_batch_cache"))  # Empty, private fixture only.
            courses = [Course(id=str(uuid4()), normalized_name=name, display_name=name) for name in ("a", "b")]
            async with sessions() as session:
                session.add_all(courses)
                await session.commit()
            await asyncio.gather(database.initialize_database_schema(), database.initialize_database_schema())
            donor, _ = await process(courses[0], allow_generation=True)
            entry = (await rows(donor))[0]
            assert donor not in json.dumps(entry.payload)
            checks.append("Concurrent additive schema upgrade preserved a pre-existing course and enabled fenced cache saves.")
            await engine.dispose()
            await database.initialize_database_schema()
            recipient, _ = await process(courses[0], allow_generation=False)
            checks.append("After reconnect and schema reinitialization, a renamed upload reused the batch with fresh source IDs and zero fixture generations.")
            other, _ = await process(courses[1], allow_generation=True)
            checks.append("Identical excerpts in a different course required a fresh fixture generation.")
            probe, task = await new_upload(courses[0])
            cache = GraphBatchCache(sessions, probe)
            assert await cache._read(entry.cache_key)
            async with sessions() as session:
                assert not await uploads.save_graph_checkpoint(session, upload_id=probe, task_id="superseded",
                    lease_owner="fixture", batch_key="stale", section_key="x", section_label="x",
                    extraction_json={}, cache_entry=CacheEntry(entry.cache_key, entry.payload, entry.digest))
            assert not await rows(probe)
            checks.append("A superseded document task could not persist a checkpoint or cache entry.")
            jobs = GraphContinuationService()
            async with sessions() as session:
                probe_record = await session.get(DocumentUpload, probe)
                parser = ParserService(ocr_enabled=False)
                evidence = parser.chunk_pages(parser.extract_pages_from_bytes(objects[probe_record.storage_key]), courses[0].id,
                    probe, probe_record.original_filename, max_chunks=20)
                for chunk in evidence:
                    chunk.metadata["execution_token"] = task
                assert await uploads.set_stage(session, probe, task, ProcessingStage.GRAPH_BUILT, lease_owner="fixture")
                assert await uploads.mark_completed(session, probe, task,
                    {"chunks_indexed": len(evidence), "nodes_upserted": 0, "relationships_upserted": 0,
                     "graph_status": "READY_WITHOUT_GRAPH", "graph_provider_limited": True},
                    lease_owner="fixture")
                job = await jobs.schedule(session, upload_id=probe, document_task_id=task,
                                          chunks=evidence, retry_after_seconds=2)
                assert job is not None
                assert await jobs.claim_next(session, lease_owner="graph", lease_seconds=60) is None
            await asyncio.sleep(2.1)
            async with sessions() as session:
                claim = await jobs.claim_next(session, lease_owner="graph", lease_seconds=60)
                assert claim and claim.upload_id == probe
            graph_ingestion = IngestionService(None, None)
            graph_ingestion.extract_graph_from_text = AsyncMock(side_effect=AssertionError("Unexpected generation"))
            resumed = await GraphJobExtraction(sessions, graph_ingestion).extract(claim, lease_seconds=60)
            assert all(node.upload_id == probe for node in resumed.nodes) and len(resumed.nodes) == 2
            assert len(await rows(probe)) == 1
            graph_ingestion.extract_graph_from_text.assert_not_awaited()
            await remove(probe)
            probe, task = await new_upload(courses[0])
            cache = GraphBatchCache(sessions, probe)
            checks.append("The graph-only job observed its real delay, then saved a rebound hit under its independent lease without generation.")
            async with sessions() as session:
                bounded = CacheEntry(entry.cache_key, entry.payload, entry.digest)
                with patch("app.services.graph_batch_cache.MAX_ENTRIES_PER_UPLOAD", 0):
                    assert await uploads.save_graph_checkpoint(session, upload_id=probe, task_id=task,
                        lease_owner="fixture", batch_key="bounded", section_key="bounded", section_label="bounded",
                        extraction_json={}, cache_entry=bounded)
                assert not await rows(probe)
            checks.append("At the per-owner cache cap, the ordinary durable checkpoint still completed.")
            await remove(donor)
            assert not await rows(donor) and await cache._read(entry.cache_key)
            await remove(recipient)
            assert not await rows(recipient) and not await cache._read(entry.cache_key) and await rows(other)
            checks.append("Deleting each owner cascaded only its own copy, removing the final same-course cache hit.")
            # Mutate only this private fixture to simulate an old task and an ineligible donor.
            probe_other, _ = await new_upload(courses[1])
            cache_other = GraphBatchCache(sessions, probe_other)
            assert await cache_other._read(entry.cache_key)
            async with sessions() as session:
                owner = await session.get(DocumentUpload, other)
                owner.status = "active"
                await session.commit()
                assert not await cache_other._read(entry.cache_key)
                owner.status = "ready"
                owner.result_json = {"deletion_requested": True}
                await session.commit()
                assert not await cache_other._read(entry.cache_key)
                owner.result_json = None
                owner.task_id = str(uuid4())
                await session.commit()
                assert not await cache_other._read(entry.cache_key)
            checks.append("Active, deletion-pending and superseded donors failed lookup.")
            await remove(other)
            async with sessions() as session:
                assert await session.scalar(select(func.count()).select_from(GraphBatchCacheRow)) == 0
            return checks
    finally:
        await engine.dispose()
        async with admin.begin() as conn:
            await conn.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        await admin.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    print(json.dumps(asyncio.run(run(parser.parse_args())), indent=2))
