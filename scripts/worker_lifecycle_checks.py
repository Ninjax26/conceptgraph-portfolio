"""Full local entry points, real stores/timer, explicitly offline AI adapters."""
import argparse
import asyncio
import contextlib
import functools
import hashlib
from contextlib import ExitStack
import json
from pathlib import Path
import signal
import socket
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx
import numpy as np
import pymupdf
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core import database, processing_coordinator as coordination, security
from app.models.document_upload import DocumentUpload, VectorDeletionTombstone, WorkerPresence
from app.services import document_processing_service as processing, ingestion_service as ingestion_module
from app.services import rag_service as retrieval_module, worker_presence_service as presence
from app.services import shared_provider_service as shared
from app.services.demo_retention_service import DemoRetentionService
from app.services.parser_service import ParserService
from app.services.security_service import DemoAccessService, RateLimitService
from app.services.vector_cleanup_service import VectorCleanupScope
from scripts.deferred_vector_proxy import DeferredVectorProxy
from scripts.evaluate_provider_admission import engine_for
from scripts.processor_crash_checks import AuthoredIngestion


def lifecycle_config(base, role, *, scoped=True):
    if (role not in {"api", "worker"} or not base.graph_projection_fencing_enabled
        or type(scoped) is not bool):
        raise ValueError("Use an opted-in private fixture with an explicit split-process role.")
    return base.model_copy(update={"processing_role": role, "shared_provider_limits_enabled": True,
        "embedding_provider": "local", "embedding_dimension": 3, "processing_concurrency": 1,
        "processing_queue_capacity": 2, "processing_dispatch_interval_seconds": .25,
        "processing_lease_seconds": 60, "processing_heartbeat_seconds": 5,
        "demo_cleanup_interval_seconds": 60, "graph_continuation_snapshots_enabled": False,
        "graph_projection_fencing_enabled": scoped})


def inventory_fixture_config(config, context):
    """Describe the actual privately injected clients, without changing env files."""
    query = urlencode({"host":context["socket_dir"],"port":context["port"]})
    return config.model_copy(update={"database_url":"postgresql+asyncpg://cg_budget@/postgres?"+query,
                                     "neo4j_uri":context["neo4j"]})


class OfflineReranker:
    async def rerank(self, question, chunks):
        return [dict(chunk, rerank_score=10.) for chunk in chunks]


class OfflineSynthesis:
    async def synthesize_with_metadata(self, *, question, graph_context, sources):
        if not sources:
            raise ValueError("An offline excerpt requires accepted evidence.")
        return SimpleNamespace(answer="Offline fixture excerpt: "+sources[0]["supporting_passage"]+" [Source 1]",
            provider_used="evidence_fallback", failover_used=False, failover_reason=None)


async def observe_stop(operation, started, completed):
    """Private observer: always await the actual stop, even if a receipt write fails."""
    with contextlib.suppress(OSError):
        started.write_text("stop.entered")
    await operation()
    with contextlib.suppress(OSError):
        completed.write_text("stop.completed")


class ShutdownReceipt:
    """Observe real ASGI shutdown; Uvicorn re-raises SIGTERM after cleanup."""
    def __init__(self, app, path):
        self.app, self.path = app, path

    async def __call__(self, scope, receive, send):
        async def observed_send(message):
            await send(message)
            if scope["type"] == "lifespan" and message["type"] == "lifespan.shutdown.complete":
                self.path.write_text("lifespan.shutdown.complete")
        await self.app(scope, receive, observed_send)


async def child(args):
    from app import main, worker
    from app.api.endpoints import ingest, query
    from qdrant_client import QdrantClient
    from scripts.evaluate_upload_deletion import clients, local_url
    context = json.loads(args.context.read_text())
    engine = engine_for(SimpleNamespace(socket_dir=Path(context["socket_dir"]), port=context["port"]), context["schema"])
    driver, vector, storage = clients(context)
    if context.get("vector_transport"):
        vector.close()
        vector = QdrantClient(url=local_url(context["vector_transport"], "http"),
            timeout=30 if context.get("drain_rehearsal") else 5,
            check_compatibility=False, cloud_inference=False)
    cfg = lifecycle_config(storage.config, args.role, scoped=context.get("cohort_scoped", True))
    if context.get("fleet_inventory"):
        cfg = inventory_fixture_config(cfg, context)
    sessions = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    ingestion = AuthoredIngestion(driver, vector, context["collection"])
    processor = processing.DocumentProcessingService(config=cfg, ingestion_service=ingestion,
        parser_service=ParserService(ocr_enabled=False))
    coordinator = coordination.ProcessingCoordinator(config=cfg, processing_service=processor)
    if context.get("drain_rehearsal"):
        coordinator.stop = functools.partial(observe_stop, coordinator.stop,
            args.context.with_suffix(f".{args.role}.stop-started"),
            args.context.with_suffix(f".{args.role}.stop-completed"))
    retention = DemoRetentionService(config=cfg, storage=storage)
    retention.ingestion_service = ingestion
    limiter = RateLimitService()
    retrieval = retrieval_module.RetrievalService(driver, vector)
    retrieval.collection_name = context["collection"]
    retrieval._embedding_model = SimpleNamespace(encode=lambda *a, **kw: np.array([1., 0., 0.]))
    from app.services import demo_retention_service as retention_module
    with ExitStack() as stack:
        replacements = {
            database: dict(postgres_engine=engine, AsyncSessionLocal=sessions, neo4j_driver=driver,
                qdrant_client=vector, settings=cfg),
            processing: dict(AsyncSessionLocal=sessions, storage_service=storage, settings=cfg),
            coordination: dict(AsyncSessionLocal=sessions),
            presence: dict(AsyncSessionLocal=sessions),
            shared: dict(AsyncSessionLocal=sessions, settings=cfg),
            retention_module: dict(AsyncSessionLocal=sessions),
            ingestion_module: dict(settings=cfg), retrieval_module: dict(settings=cfg),
            security: dict(settings=cfg, demo_access_service=DemoAccessService(cfg), rate_limit_service=limiter),
            ingest: dict(settings=cfg, storage_service=storage, document_processing_service=processor,
                processing_coordinator=coordinator),
            main: dict(settings=cfg, postgres_engine=engine, neo4j_driver=driver, qdrant_client=vector,
                storage_service=storage, document_processing_service=processor, processing_coordinator=coordinator,
                demo_retention_service=retention, rate_limit_service=limiter),
            worker: dict(settings=cfg, document_processing_service=processor, processing_coordinator=coordinator,
                demo_retention_service=retention),
        }
        for module, values in replacements.items():
            for name, value in values.items():
                stack.enter_context(patch.object(module, name, value))
        stack.enter_context(patch.object(query, "get_rerank_service", lambda: OfflineReranker()))
        stack.enter_context(patch.object(query, "get_synthesis_service", lambda: OfflineSynthesis()))
        main.app.dependency_overrides[query.get_retrieval_service] = lambda: retrieval
        if context.get("fleet_inventory"):
            from app.services.rollout_inventory import collect_inventory
            names = ("app/core/database.py","app/core/processing_coordinator.py","app/worker.py","app/main.py",
                "app/services/ingestion_service.py","app/services/rag_service.py",
                "app/services/rollout_inventory.py","scripts/worker_lifecycle_checks.py")
            code_digest = hashlib.sha256(json.dumps({name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest()
                for name in names},sort_keys=True).encode()).hexdigest()
            inventory = await collect_inventory(cfg, engine, driver, vector, storage, code_sha256=code_digest)
            inventory_path = args.context.with_suffix(f".{args.role}.inventory.json")
            inventory_path.write_text(json.dumps(inventory))
            inventory_path.chmod(0o600)
        if args.role == "worker":
            await worker.run_worker()
        else:
            import uvicorn
            listener = socket.socket(fileno=args.listener_fd)
            if listener.getsockname()[0] != "127.0.0.1":
                raise ValueError("The fixture API must bind only loopback.")
            observed = ShutdownReceipt(main.app, args.context.parent/"api-lifespan-closed")
            server = uvicorn.Server(uvicorn.Config(observed, log_level="error", access_log=False, lifespan="on"))
            await server.serve(sockets=[listener])


async def verify_lifecycle(context, sessions, driver, vector, storage):
    from qdrant_client import models
    from scripts.evaluate_upload_deletion import TOKEN
    checks, observations, processes = {}, {}, []
    headers = {"Authorization": "Bearer "+TOKEN}
    source_course = "Lifecycle "+uuid4().hex
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_textbox((60, 70, 530, 500),
            "DNS Resolution\nDNS translates a domain name into an IP address before a browser sends packets. "
            "The browser needs that address to reach the server. TCP provides a connection and TLS protects it. "
            "The equipment PMP-104 requires monthly inspection. This synthetic native PDF is used only "
            "to test local API and worker lifecycle boundaries, not model accuracy.", fontsize=12)
        content = pdf.tobytes()

    async def finish(process, expected=0):
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), 20)
        except BaseException:
            if process.returncode is None:
                process.kill()
            await process.communicate()
            raise
        if process.returncode != expected:
            raise RuntimeError(f"Lifecycle process exited {process.returncode}, expected {expected}: {stderr.decode()[-1800:]}")

    async def wait_until(predicate, label, *, timeout=25):
        async with asyncio.timeout(timeout):
            while True:
                for process in processes:
                    if process.returncode is not None and process not in stopped:
                        await finish(process)
                        raise AssertionError("Lifecycle process ended unexpectedly: "+label)
                value = await predicate()
                if value:
                    return value
                await asyncio.sleep(.2)

    async def count(upload):
        return await asyncio.to_thread(lambda: vector.count(context["collection"], count_filter=models.Filter(
            must=[models.FieldCondition(key="upload_id", match=models.MatchValue(value=upload))]), exact=True).count)

    async def record(upload):
        async with sessions() as session:
            return await session.get(DocumentUpload, upload)

    async def concepts(upload):
        async with driver.session() as session:
            result = await session.run("MATCH (n:Concept {upload_id:$upload}) RETURN count(n) AS n", upload=upload)
            return (await result.single())["n"]

    stopped = set()
    with tempfile.TemporaryDirectory(prefix="cg-full-lifecycle-", dir="/private/tmp") as directory:
        manifest = Path(directory)/"context.json"
        manifest.write_text(json.dumps(context))
        manifest.chmod(0o600)
        async def launch(role, *, transport=None, listener=None):
            path = manifest
            if transport:
                path = Path(directory)/"transport.json"
                path.write_text(json.dumps({**context, "vector_transport": transport}))
                path.chmod(0o600)
            command = [sys.executable, str(Path(__file__).resolve()), "--context", str(path), "--role", role]
            descriptors = ()
            if listener is not None:
                command += ["--listener-fd", str(listener.fileno())]
                descriptors = (listener.fileno(),)
            process = await asyncio.create_subprocess_exec(*command, cwd="/private/tmp", pass_fds=descriptors,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            processes.append(process)
            return process
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        base = f"http://127.0.0.1:{listener.getsockname()[1]}"
        try:
            api = await launch("api", listener=listener)
            listener.close()
            async with httpx.AsyncClient(base_url=base, trust_env=False, timeout=3) as client:
                async def ready():
                    try:
                        response = await client.get("/api/v1/ready")
                        return response.json() if response.status_code == 200 else None
                    except httpx.HTTPError:
                        return None
                status = await wait_until(ready, "API startup")
                assert status["processing_role"] == "api" and not status["ingestion_available"]
                assert "ingestion_worker" in status["degraded_services"]
                assert (await client.get("/api/v1/ingest/uploads")).status_code == 401
                response = await client.post("/api/v1/ingest/upload", headers=headers,
                    data={"course_id": source_course}, files={"file": ("lifecycle-fixture.pdf", content, "application/pdf")})
                assert response.status_code == 202, response.text
                uploaded = response.json()
                upload, original_task, course = uploaded["upload_id"], uploaded["task_id"], uploaded["course_id"]
                assert (await record(upload)).stage == "UPLOADED" and await count(upload) == 0
                question = {"course_id": course, "question": "What does DNS translate?", "retrieval_mode": "vector_only"}
                assert (await client.post("/api/v1/query", json=question)).status_code == 401
                assert (await client.post("/api/v1/query", headers=headers, json=question)).status_code == 409
                checks["real_api_defers_ingestion_without_worker_and_enforces_auth_and_ready_gate"] = True

                async with DeferredVectorProxy(context["qdrant"], context["collection"], upload, original_task) as proxy:
                    old_worker = await launch("worker", transport=proxy.url)
                    async def captured():
                        return proxy.captured.result() if proxy.captured.done() else None
                    captured_put = await wait_until(captured, "worker accepted PUT")
                    current = await record(upload)
                    assert current.stage == "EMBEDDING"
                    assert (await client.delete(f"/api/v1/ingest/uploads/{upload}", headers=headers)).status_code == 409
                    old_worker.kill()
                    await finish(old_worker, -signal.SIGKILL)
                    stopped.add(old_worker)
                    async with sessions() as session:
                        await session.execute(update(DocumentUpload).where(DocumentUpload.upload_id == upload).values(
                            lease_expires_at=func.now()))
                        await session.execute(update(WorkerPresence).where(WorkerPresence.instance_id == current.lease_owner).values(
                            expires_at=func.now()))
                        await session.commit()
                    assert not (await ready())["ingestion_available"]
                    checks["full_worker_sigkill_preserves_active_delete_guard_and_expired_presence_is_unavailable"] = True
                    worker = await launch("worker")
                    async def recovered():
                        row = await record(upload)
                        if row.status == "failed":
                            raise AssertionError("Full worker failed: "+str(row.error_message))
                        return row if row.status == "ready" else None
                    current = await wait_until(recovered, "full worker recovery")
                    assert current.attempt_count == 2 and current.task_id != original_task
                    status = await ready()
                    assert status["ingestion_available"] and status["active_workers"] == 1
                    assert status["processing_queue_depth"] == 0
                    if context.get("fleet_inventory"):
                        from scripts.fleet_inventory_checks import verify_inventory
                        inventories = [json.loads(manifest.with_suffix(f".{role}.inventory.json").read_text())
                            for role in ("api","worker")]
                        assert [m["pid"] for m in inventories] == [api.pid,worker.pid]
                        inventory_result = await verify_inventory(context, sessions, driver, vector, storage, inventories)
                        checks.update(inventory_result["checks"])
                        observations["fleet_inventory"] = inventory_result["observations"]
                    answer = await client.post("/api/v1/query", headers=headers, json=question)
                    assert answer.status_code == 200, answer.text
                    payload = answer.json()
                    assert payload["sources"] and all(s["document_id"] == upload for s in payload["sources"])
                    assert payload["generation_metadata"]["provider_used"] == "evidence_fallback"
                    preview = await client.get(f"/api/v1/ingest/uploads/{upload}/preview",
                        headers={**headers, "Range": "bytes=0-15"})
                    assert preview.status_code == 206 and preview.content.startswith(b"%PDF-")
                    checks["full_worker_recovers_and_http_query_citations_and_range_preview_use_ready_evidence"] = True

                    neighbor_response = await client.post("/api/v1/ingest/upload", headers=headers,
                        data={"course_id": source_course+" neighbor"}, files={"file": ("neighbor.pdf", content, "application/pdf")})
                    assert neighbor_response.status_code == 202
                    neighbor = neighbor_response.json()["upload_id"]
                    async def neighbor_ready():
                        row = await record(neighbor)
                        return row if row.status == "ready" else None
                    neighbor_record = await wait_until(neighbor_ready, "neighbor ingestion")
                    neighbor_count = await count(neighbor)
                    neighbor_concepts = await concepts(neighbor)
                    assert neighbor_count > 0 and neighbor_concepts > 0
                    assert await concepts(upload) > 0 and await asyncio.to_thread(storage.exists, current.storage_key)
                    assert (await client.delete(f"/api/v1/ingest/uploads/{upload}", headers=headers)).status_code == 204
                    assert await count(upload) == 0 and await record(upload) is None
                    assert await concepts(upload) == 0 and not await asyncio.to_thread(storage.exists, current.storage_key)
                    scope = VectorCleanupScope.from_url(context["qdrant"], context["collection"])
                    async def empty_timer_check():
                        async with sessions() as session:
                            rows = (await session.execute(select(VectorDeletionTombstone).where(
                                VectorDeletionTombstone.upload_id == upload))).scalars().all()
                            assert {row.task_id for row in rows} == {original_task, current.task_id}
                            assert all(row.store_sha256 == scope.store_sha256 for row in rows)
                            return all(row.last_checked_at is not None for row in rows)
                    timer_started = time.perf_counter()
                    await wait_until(empty_timer_check, "automatic empty retention check", timeout=80)
                    empty_wait = round(time.perf_counter()-timer_started, 3)
                    delivery = await proxy.release()
                    assert delivery["completed"] and delivery["body_sha256"] == captured_put["body_sha256"]
                    assert await count(upload) == 1
                    assert (await client.post("/api/v1/query", headers=headers, json=question)).status_code == 404
                    assert (await client.get(f"/api/v1/ingest/uploads/{upload}/preview", headers=headers)).status_code == 404
                    async with sessions() as session:
                        await session.execute(update(VectorDeletionTombstone).where(
                            VectorDeletionTombstone.upload_id == upload).values(next_attempt_at=func.now()))
                        await session.commit()
                    timer_started = time.perf_counter()
                    async def no_vectors():
                        return await count(upload) == 0
                    await wait_until(no_vectors, "automatic late-write cleanup", timeout=80)
                    cleanup_wait = round(time.perf_counter()-timer_started, 3)
                    assert await count(neighbor) == neighbor_count
                    assert await concepts(upload) == 0 and not await asyncio.to_thread(storage.exists, current.storage_key)
                    assert (await record(neighbor)).status == "ready" and await concepts(neighbor) == neighbor_concepts
                    assert await asyncio.to_thread(storage.exists, neighbor_record.storage_key)
                    checks["real_retention_timer_keeps_empty_tracking_then_cleans_post_delete_delayed_put"] = True
                    checks["deleted_source_stays_inaccessible_and_four_stores_preserve_neighbor_upload"] = True
                    assert (await client.delete(f"/api/v1/ingest/uploads/{neighbor}", headers=headers)).status_code == 204
                    worker.send_signal(signal.SIGTERM)
                    await finish(worker)
                    stopped.add(worker)
                    async with sessions() as session:
                        assert list((await session.execute(select(WorkerPresence))).scalars()) == []
                    assert not (await ready())["ingestion_available"]
                    checks["sigterm_drains_full_worker_and_removes_registry_presence"] = True
                    observations.update({"empty_timer_wait_seconds": empty_wait,
                        "late_cleanup_timer_wait_seconds": cleanup_wait, "configured_cleanup_interval_seconds": 60,
                        "original_worker_exit_code": old_worker.returncode, "replacement_worker_exit_code": worker.returncode,
                        "lease_and_presence_expiry_advanced_in_owned_fixture": True,
                        "cleanup_due_time_advanced_in_owned_fixture": True,
                        "manual_sweep_calls": 0, "manual_vector_cleanup_calls": 0,
                        "hosted_model_calls": 0, "transport_only_proxy_kept_logical_store_binding_identical": True})
                api.send_signal(signal.SIGTERM)
                await finish(api, -signal.SIGTERM)
                stopped.add(api)
                assert (Path(directory)/"api-lifespan-closed").read_text() == "lifespan.shutdown.complete"
                observations["api_exit_code_after_completed_asgi_shutdown"] = api.returncode
                checks["sigterm_completes_actual_api_lifespan_before_uvicorn_reraises_signal"] = True
        finally:
            listener.close()
            for process in processes:
                if process.returncode is None:
                    process.kill()
                    await process.communicate()
    return {"checks": checks, "observations": observations}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--context", type=Path, required=True)
    parser.add_argument("--role", choices=("api", "worker"), required=True)
    parser.add_argument("--listener-fd", type=int)
    asyncio.run(child(parser.parse_args()))
