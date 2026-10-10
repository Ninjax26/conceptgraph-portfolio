"""Measure isolated ingestion with local PostgreSQL and cached CPU embeddings.

Uses the real upload endpoint, coordinator, leases, parser, state machine and
in-memory Qdrant. Object storage and graph generation/projection are test doubles.
No hosted LLM, Neo4j or R2 request is sent. Never targets a remote database.
"""

import argparse
import asyncio
from collections import Counter
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4
import warnings

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.evaluate_retrieval_scaling import equipment_pdf, percentile


def require_local_database(dsn):
    if urlsplit(dsn).hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("This benchmark only permits local PostgreSQL, never a hosted database.")


class FixtureStorage:
    """Thread-safe, isolated source byte inventory; not an R2 benchmark."""
    def __init__(self):
        self.objects = {}
        self.lock = threading.Lock()

    def put_pdf(self, key, content):
        with self.lock:
            self.objects[key] = content

    def get_bytes(self, key):
        with self.lock:
            return self.objects[key]

    def exists(self, key):
        with self.lock:
            return key in self.objects

    def delete(self, key):
        with self.lock:
            self.objects.pop(key, None)


async def run(args):
    import httpx
    import torch
    from fastapi import FastAPI
    from qdrant_client import QdrantClient
    from sentence_transformers import SentenceTransformer
    from sqlalchemy import select, text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from app.api.endpoints import ingest
    from app.core import database, processing_coordinator as coordinator_module
    from app.core.config import Settings, settings
    from app.core.processing_coordinator import ProcessingCoordinator
    from app.models.document_upload import DocumentUpload
    from app.schemas.extraction import GraphExtractionResponse
    from app.services import document_processing_service as processor_module
    from app.services import shared_provider_service as shared
    from app.services.document_processing_service import DocumentProcessingService
    from app.services.ingestion_service import IngestionService
    from app.services.rag_service import RetrievalService
    from app.services.resource_budget_service import resource_budget_service
    from app.services.upload_service import UploadService

    require_local_database(settings.postgres_dsn)
    schema = "cg_scale_" + uuid4().hex
    admin = create_async_engine(settings.postgres_dsn, pool_size=1, max_overflow=0)
    engine = None
    storage = FixtureStorage()
    torch.set_num_threads(2)
    model = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2", device="cpu", local_files_only=True)
    model.encode(["Warmup"], normalize_embeddings=True)
    print("Cached CPU model loaded. Local database schema isolated; external providers/stores disabled.", flush=True)
    reports = []
    checks = {}
    started_coordinators = []
    warnings.filterwarnings("ignore", message="Payload indexes have no effect.*")
    try:
        async with admin.begin() as connection:
            await connection.execute(text(f"CREATE SCHEMA {schema}"))
        engine = create_async_engine(settings.postgres_dsn, pool_size=5, max_overflow=3,
                                     connect_args={"server_settings": {"search_path": schema}})
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        with patch.object(database, "postgres_engine", engine):
            await asyncio.gather(database.initialize_database_schema(), database.initialize_database_schema())
        checks["concurrent_schema_initialization"] = True
        config = Settings(_env_file=None, MAX_PDFS_PER_INSTALLATION=0,
                          PROCESSING_QUEUE_CAPACITY=8, PROCESSING_CONCURRENCY=1,
                          PROCESSING_DISPATCH_INTERVAL_SECONDS=0.25, MAX_PENDING_DOCUMENTS=32)

        async def dependency():
            async with sessions() as session:
                yield session

        api = FastAPI()
        api.include_router(ingest.router)
        api.dependency_overrides[database.get_postgres_session] = dependency
        counter = Counter()

        class FixtureIngestion(IngestionService):
            def __init__(self, vectors):
                super().__init__(SimpleNamespace(), vectors)
                self._embedding_model = model
                self.projected = set()
                self.pause = False
                self.checkpoint_written = asyncio.Event()
                self.release = asyncio.Event()
                self.resumed = False

            async def extract_graph_from_chunks(self, chunks, *, completed_batches=None, on_batch_completed=None):
                if self.pause:
                    from app.services.ingestion_service import GraphSectionBatch
                    if completed_batches and "fixture-checkpoint" in completed_batches:
                        self.resumed = True
                    else:
                        batch = GraphSectionBatch("fixture-checkpoint", "fixture", "Fixture", tuple(chunks))
                        await on_batch_completed(batch, GraphExtractionResponse(nodes=[], relationships=[]))
                        self.checkpoint_written.set()
                        await self.release.wait()
                return GraphExtractionResponse(nodes=[], relationships=[])

            async def store_graph_extraction(self, graph, course_id, **kwargs):
                self.projected.add(kwargs["upload_id"])

            async def cleanup_graph(self, upload_id, course_id):
                self.projected.discard(upload_id)

        class MeasuredProcessor(DocumentProcessingService):
            async def process_document(self, upload_id, task_id, **kwargs):
                counter[(upload_id, task_id)] += 1
                return await super().process_document(upload_id, task_id, **kwargs)

        with ExitStack() as stack:
            stack.enter_context(patch.object(coordinator_module, "AsyncSessionLocal", sessions))
            stack.enter_context(patch.object(processor_module, "AsyncSessionLocal", sessions))
            stack.enter_context(patch.object(processor_module, "storage_service", storage))
            stack.enter_context(patch.object(ingest, "storage_service", storage))
            stack.enter_context(patch.object(resource_budget_service, "config", config))
            # API-only admission persists jobs; workers discover them in PostgreSQL.
            stack.enter_context(patch.object(ingest, "processing_coordinator", SimpleNamespace(submit=async_deferred)))
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://fixture") as client:
                for count in args.documents:
                    vectors = QdrantClient(":memory:")
                    service = FixtureIngestion(vectors)
                    processor = MeasuredProcessor(config=config, ingestion_service=service)
                    stack.enter_context(patch.object(ingest, "document_processing_service", processor))
                    workers = [ProcessingCoordinator(config=config, processing_service=processor) for _ in range(args.workers)]
                    for worker in workers:
                        await worker.start()
                        started_coordinators.append(worker)
                    corpus = [equipment_pdf({"id": f"PMP-{200 + index}", "pressure": 8 + index % 20,
                                              "interval": 7 + index % 120}) for index in range(count)]
                    semaphore = asyncio.Semaphore(8)
                    admissions = Counter()

                    async def upload(document):
                        filename, content = document
                        async with semaphore:
                            deadline = time.monotonic() + 180
                            while True:
                                response = await client.post("/api/v1/ingest/upload", data={"course_id": "Scale fixture"},
                                                             files={"file": (filename, content, "application/pdf")})
                                admissions[response.status_code] += 1
                                if response.status_code == 202:
                                    return response.json()["upload_id"]
                                if response.status_code != 429 or time.monotonic() >= deadline:
                                    raise RuntimeError(f"Fixture upload failed with HTTP {response.status_code}")
                                await asyncio.sleep(0.1)

                    indexing_started = time.perf_counter()
                    ids = await asyncio.gather(*(upload(document) for document in corpus))
                    deadline = time.monotonic() + 180
                    while True:
                        async with sessions() as session:
                            rows = list((await session.execute(select(DocumentUpload))).scalars().all())
                        failed = [row for row in rows if row.status == "failed"]
                        if failed:
                            raise RuntimeError(f"Fixture processing failed: {failed[0].failure_category}")
                        if len(rows) == count and all(row.status == "ready" for row in rows):
                            break
                        if time.monotonic() >= deadline:
                            raise TimeoutError("Fixture processing did not finish within its benchmark deadline.")
                        await asyncio.sleep(0.05)
                    indexing_seconds = time.perf_counter() - indexing_started
                    retrieval = RetrievalService(SimpleNamespace(), vectors)
                    retrieval._embedding_model = model
                    async with sessions() as session:
                        context = await ingest.course_service.get_ready_context(session, rows[0].course_uuid)
                    checks[f"ready_scope_{count}"] = set(context.document_ids) == set(ids)
                    latencies = []
                    for _ in range(5):
                        tick = time.perf_counter()
                        found = await retrieval.retrieve("What is the operating pressure of PMP-200?",
                            context, retrieval_mode="vector_only")
                        latencies.append(time.perf_counter() - tick)
                        if not found["chunks"] or any(hit["metadata"]["upload_id"] not in ids for hit in found["chunks"]):
                            raise RuntimeError("READY-scoped fixture retrieval failed.")
                    fetched = []
                    cursor = None
                    tick = time.perf_counter()
                    while True:
                        params = {"limit": 7, **({"cursor": cursor} if cursor else {})}
                        response = await client.get("/api/v1/ingest/uploads/page", params=params)
                        if response.status_code != 200:
                            raise RuntimeError("Paginated fixture list failed.")
                        page = response.json()
                        fetched.extend(item["upload_id"] for item in page["items"])
                        cursor = page["next_cursor"]
                        if not cursor:
                            break
                    checks[f"pagination_{count}"] = len(fetched) == count and set(fetched) == set(ids)
                    waits = [row.result_json["queue_wait_seconds"] for row in rows]
                    reports.append({
                        "documents": count, "workers": args.workers,
                        "pages": sum(row.page_count for row in rows), "chunks": sum(row.processed_chunk_count for row in rows),
                        "source_storage_bytes": sum(row.source_bytes for row in rows),
                        "vector_bytes_estimate": sum(row.processed_chunk_count for row in rows) * 384 * 4,
                        "admission_status_counts": dict(admissions), "total_ingestion_seconds": round(indexing_seconds, 3),
                        "queue_wait_p50_seconds": round(percentile(waits, .5), 3), "queue_wait_p95_seconds": round(percentile(waits, .95), 3),
                        "vector_indexing_seconds_sum": round(sum(row.result_json["stage_seconds"]["vector_indexing"] for row in rows), 3),
                        "retrieval_p50_seconds": round(percentile(latencies, .5), 4), "retrieval_p95_seconds": round(percentile(latencies, .95), 4),
                        "pagination_scan_seconds": round(time.perf_counter() - tick, 3),
                        "graph_provider_calls": sum(row.result_json["graph_provider_calls"] for row in rows),
                        "duplicate_executions": sum(max(0, counter[(row.upload_id, row.task_id)] - 1) for row in rows),
                        "corpus_sha256": {name: hashlib.sha256(content).hexdigest() for name, content in corpus},
                    })
                    for worker in workers:
                        await worker.stop()
                        started_coordinators.remove(worker)
                    for upload_id in ids:
                        response = await client.delete(f"/api/v1/ingest/uploads/{upload_id}")
                        if response.status_code != 204:
                            raise RuntimeError("Fixture deletion failed.")
                    async with sessions() as session:
                        leftovers = int((await session.execute(text("SELECT count(*) FROM document_uploads"))).scalar_one())
                        attempts = int((await session.execute(text("SELECT count(*) FROM processing_attempts"))).scalar_one())
                        courses = int((await session.execute(text("SELECT count(*) FROM courses"))).scalar_one())
                    checks[f"cleanup_{count}"] = (leftovers == attempts == courses == 0 and not storage.objects and
                                                 not service.projected and vectors.count(service.collection_name).count == 0)
                    print(json.dumps({key: value for key, value in reports[-1].items() if key != "corpus_sha256"}), flush=True)
                    vectors.close()
                # Restart while an async graph checkpoint is durable.
                vectors = QdrantClient(":memory:")
                service = FixtureIngestion(vectors)
                service.pause = True
                processor = MeasuredProcessor(config=config, ingestion_service=service)
                stack.enter_context(patch.object(ingest, "document_processing_service", processor))
                document = equipment_pdf({"id": "PMP-999", "pressure": 12, "interval": 30})
                upload_id = (await upload(document))
                async with sessions() as session:
                    original_task = (await session.get(DocumentUpload, upload_id)).task_id
                worker = ProcessingCoordinator(config=config, processing_service=processor)
                await worker.start()
                started_coordinators.append(worker)
                await asyncio.wait_for(service.checkpoint_written.wait(), timeout=30)
                await worker.stop()
                started_coordinators.remove(worker)
                restarted = ProcessingCoordinator(config=config, processing_service=processor)
                await restarted.start()
                started_coordinators.append(restarted)
                deadline = time.monotonic() + 30
                while True:
                    async with sessions() as session:
                        row = await session.get(DocumentUpload, upload_id)
                        ready = row.status == "ready"
                        attempt_number = row.attempt_count
                    if ready:
                        break
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Restarted fixture worker did not finish.")
                    await asyncio.sleep(.05)
                checks["restart_resumed_checkpoint"] = service.resumed and attempt_number == 2
                async with sessions() as session:
                    checks["superseded_attempt_cannot_complete"] = not await UploadService().mark_completed(session, upload_id, original_task, {})
                await restarted.stop()
                started_coordinators.remove(restarted)
                await client.delete(f"/api/v1/ingest/uploads/{upload_id}")
                vectors.close()
            shared_settings = Settings(_env_file=None, SHARED_PROVIDER_LIMITS_ENABLED=True,
                                       SHARED_PROVIDER_SCOPE=schema, PROVIDER_REQUESTS_PER_MINUTE=10, PROVIDER_REQUESTS_PER_DAY=10)
            stack.enter_context(patch.object(shared, "settings", shared_settings))
            stack.enter_context(patch.object(shared, "AsyncSessionLocal", sessions))
            async def reserve():
                from app.core.exceptions import LLMProviderRateLimitError
                try:
                    await shared.reserve_provider_request("groq")
                    return True
                except LLMProviderRateLimitError:
                    return False
            reservations = await asyncio.gather(*(reserve() for _ in range(40)))
            checks["shared_provider_atomic_budget"] = sum(reservations) == 10
    finally:
        for worker in started_coordinators:
            await worker.stop()
        if engine is not None:
            await engine.dispose()
        if re.fullmatch(r"cg_scale_[0-9a-f]{32}", schema):
            async with admin.begin() as connection:
                await connection.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        await admin.dispose()
        await database.close_database_connections()
    report = {"generated_at": datetime.now(timezone.utc).isoformat(), "corpora": reports, "checks": checks,
              "scope": "Isolated ASGI upload/delete endpoints, local PostgreSQL leases/checkpoints, cached MiniLM CPU embeddings and in-memory Qdrant. Sources and graph projection are test doubles; graph LLM requests are disabled.",
              "limitations": ["Worker coordinators share a Python process but use separate identities and concurrent PostgreSQL transactions.",
                              "Restart is graceful cancellation and durable checkpoint recovery, not a hard-kill test of remote in-flight writes.",
                              "No live Neo4j, R2, provider latency, hosted ANN, TCP or production load is measured.",
                              "Source storage is exact fixture bytes; vector bytes are a float32 estimate, not full persisted-index overhead.",
                              "Five warm vector-only retrieval samples per corpus; no reranking or generation performance claim."]}
    output = ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.with_suffix(".json").write_text(json.dumps(report, indent=2) + "\n")
    lines = ["# Ingestion scaling check", "", report["scope"], "",
             "| PDFs | Pages | Chunks | Source bytes | Workers | Total ingestion s | Queue p95 s | Indexing sum s | Retrieval p95 s | Graph calls |",
             "| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for row in reports:
        fields = ["documents", "pages", "chunks", "source_storage_bytes", "workers", "total_ingestion_seconds",
                  "queue_wait_p95_seconds", "vector_indexing_seconds_sum", "retrieval_p95_seconds", "graph_provider_calls"]
        lines.append("| " + " | ".join(str(row[field]) for field in fields) + " |")
    lines += ["", "## Checks", ""] + [f"- {name}: {'passed' if value else 'FAILED'}" for name, value in checks.items()]
    lines += ["", "## Limits", ""] + [f"- {value}" for value in report["limitations"]]
    output.with_suffix(".md").write_text("\n".join(lines) + "\n")
    if not all(checks.values()) or any(row["duplicate_executions"] for row in reports):
        raise SystemExit("An isolated consistency check failed; inspect the report.")


async def async_deferred(*args):
    from app.core.processing_coordinator import EnqueueDisposition
    return EnqueueDisposition.DEFERRED


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--documents", type=int, nargs="+", default=[10, 50, 100])
    parser.add_argument("--workers", type=int, choices=range(1, 5), default=2)
    parser.add_argument("--output", default="evaluation/ingestion-scaling-current")
    args = parser.parse_args()
    if any(count < 1 or count > 500 for count in args.documents):
        parser.error("Use between 1 and 500 fixture PDFs per corpus.")
    asyncio.run(run(args))
