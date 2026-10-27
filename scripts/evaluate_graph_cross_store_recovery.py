"""Isolated PostgreSQL/Neo4j deletion recovery with a hard-killed process.

The caller supplies a private temporary PostgreSQL socket. This script creates
and removes its own PostgreSQL schema and loopback-only Neo4j container/network.
No hosted provider, production storage, Qdrant or API endpoint is contacted.
"""

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from neo4j import AsyncGraphDatabase
from neo4j.exceptions import ServiceUnavailable
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core import database
from app.models.document_upload import Course, DocumentUpload, GraphPublicationCleanupReceipt, GraphPublicationIntent
from app.services.graph_continuation_service import GraphContinuationService
from app.services.demo_retention_service import DemoRetentionService
from app.services.ingestion_service import GraphProjectionMarker, IngestionService
from scripts.evaluate_graph_projection import docker, graph, rows
from scripts.evaluate_graph_retry import fixture
from scripts.evaluate_provider_admission import engine_for


async def killed_child(args):
    engine = engine_for(args, args.schema)
    driver = AsyncGraphDatabase.driver(args.neo4j_uri, auth=None)
    sessions = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    real = IngestionService(graph_driver=driver, vector_client=SimpleNamespace())

    class KillAfterRemoteCommit:
        async def tombstone_graph_publication(self, upload_id, course_id):
            await real.tombstone_graph_publication(upload_id, course_id)
            os._exit(137)  # PostgreSQL's still-open receipt transaction dies.

    try:
        await GraphContinuationService.reconcile_deleted_source_publication(
            sessions, args.intent_id, KillAfterRemoteCommit())
        raise AssertionError("The hard-kill fixture did not reach Neo4j")
    finally:
        await driver.close()
        await engine.dispose()


async def run(args):
    started = time.perf_counter()
    token = uuid4().hex
    schema = "cg_admission_" + token
    label = f"conceptgraph.disposable-recovery={token}"
    name = f"cg-recovery-test-{token}"
    network = container = driver = engine = None
    admin = engine_for(args)
    schema_created = False
    checks = {}
    try:
        image_id = docker("image", "inspect", "neo4j:5-community", "--format", "{{.Id}}")
        network = docker("network", "create", "--label", label, name)
        container = docker("run", "--detach", "--pull=never", "--name", name,
            "--label", label, "--network", network, "--publish", "127.0.0.1::7687",
            "--memory", "1g", "--cpus", "1", "--tmpfs", "/data:rw,size=256m",
            "--env", "NEO4J_AUTH=none", "--env", "NEO4J_server_memory_heap_initial__size=256m",
            "--env", "NEO4J_server_memory_heap_max__size=256m",
            "--env", "NEO4J_dbms_usage__report_enabled=false",
            "--env", "NEO4J_db_tx__log_preallocate=false",
            "--env", "NEO4J_db_tx__log_rotation_size=16M",
            "--env", "NEO4J_server_memory_pagecache_size=64m", image_id)
        binding = docker("port", container, "7687/tcp")
        if not binding.startswith("127.0.0.1:") or "\n" in binding:
            raise RuntimeError("Test Neo4j must bind only loopback")
        uri = f"bolt://{binding}"
        driver = AsyncGraphDatabase.driver(uri, auth=None, connection_timeout=2)
        deadline = time.monotonic() + 90
        while True:
            try:
                await driver.verify_connectivity()
                break
            except ServiceUnavailable:
                if time.monotonic() >= deadline:
                    raise
                await asyncio.sleep(1)

        async with admin.begin() as connection:
            await connection.execute(text(f"CREATE SCHEMA {schema}"))
            schema_created = True
        engine = engine_for(args, schema)
        sessions = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        with patch.object(database, "postgres_engine", engine):
            await database.initialize_database_schema()
        course = Course(id=str(uuid4()), normalized_name="isolated recovery fixture",
            display_name="Isolated recovery fixture")
        async with sessions() as session:
            session.add(course)
            await session.commit()

        upload_id, task_id, _ = await fixture(sessions, course)
        intent_id, job_id = str(uuid4()), str(uuid4())
        async with sessions() as session:
            session.add(GraphPublicationIntent(id=intent_id, job_id=job_id,
                upload_id=upload_id, document_task_id=task_id,
                course_id=course.id, payload_sha256="a" * 64, source_generation=1))
            await session.commit()
        ingestion = IngestionService(graph_driver=driver, vector_client=SimpleNamespace())
        marker = GraphProjectionMarker(intent_id, job_id, upload_id, course.id,
            task_id, "a" * 64, 1)
        await ingestion.replace_graph_extraction(graph(upload_id), course.id,
            upload_id=upload_id, document_name="fixture.pdf", course_name=course.display_name,
            execution_token=task_id, marker=marker)
        assert await ingestion.read_graph_publication_marker(intent_id) == marker
        checks["remote_publication_marker_committed_before_source_deletion"] = True

        async with sessions() as session:
            await session.execute(delete(DocumentUpload).where(DocumentUpload.upload_id == upload_id))
            await session.commit()
        child = await asyncio.create_subprocess_exec(sys.executable, str(Path(__file__).resolve()),
            "--child", "--socket-dir", str(args.socket_dir), "--port", str(args.port),
            "--schema", schema, "--intent-id", intent_id, "--neo4j-uri", uri,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            _, stderr = await asyncio.wait_for(child.communicate(), 30)
            assert child.returncode == 137, f"Hard-kill child did not stop at fixture boundary: {stderr[-500:]}"
        finally:
            if child.returncode is None:
                child.kill()
                await child.wait()
        assert (await rows(driver, "MATCH (f:GraphProjectionFence {upload_id:$id}) "
            "RETURN f.deleted AS deleted", id=upload_id)) == [{"deleted": True}]
        assert (await rows(driver, "MATCH (n:Concept {upload_id:$id}) RETURN count(n) AS n",
            id=upload_id))[0]["n"] == 0
        async with sessions() as session:
            assert (await session.execute(select(func.count()).select_from(
                GraphPublicationCleanupReceipt))).scalar_one() == 0
            discovered = await GraphContinuationService.list_deleted_source_intents(session)
            assert [row.id for row in discovered] == [intent_id]
        checks["hard_kill_after_neo4j_commit_leaves_durable_postgresql_retry"] = True

        retention = DemoRetentionService()
        retention.ingestion_service = ingestion
        with patch("app.services.demo_retention_service.AsyncSessionLocal", sessions):
            assert await retention.cleanup_deleted_graph_publications_once() == 1
            assert await retention.cleanup_deleted_graph_publications_once() == 0
        async with sessions() as session:
            assert (await session.execute(select(func.count()).select_from(
                GraphPublicationCleanupReceipt).where(
                GraphPublicationCleanupReceipt.intent_id == intent_id))).scalar_one() == 1
            assert await GraphContinuationService.list_deleted_source_intents(session) == []
        assert await ingestion.read_graph_publication_marker(intent_id) is None
        checks["retention_sweep_finishes_receipt_idempotently_across_real_stores"] = True
    finally:
        if driver is not None:
            await driver.close()
        if engine is not None:
            await engine.dispose()
        if schema_created:
            async with admin.begin() as connection:
                await connection.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        await admin.dispose()
        if container is not None:
            docker("stop", "--time", "10", container)
            docker("rm", "--volumes", container)
        if network is not None:
            docker("network", "rm", network)
    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "kind": "disposable real PostgreSQL plus Neo4j hard-kill recovery checks",
        "checks": checks, "passed": all(checks.values()),
        "hosted_provider_calls": 0, "production_test_data_mutations": 0,
        "test_schema_container_network_removed": True,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "source_sha256": {str(path): hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
            for path in ("app/services/graph_continuation_service.py",
                "app/services/demo_retention_service.py",
                "app/services/ingestion_service.py", "app/models/document_upload.py",
                "scripts/evaluate_graph_cross_store_recovery.py")},
        "limits": ["Source deletion is simulated in isolated PostgreSQL; the live API/R2/Qdrant path is not exercised.",
            "The hard kill occurs immediately after Neo4j tombstone commit, before PostgreSQL receipt commit; other crash boundaries remain untested.",
            "The live combined API schedules deleted-intent cleanup, but this run does not exercise its full R2/Qdrant deletion path.",
            "Automatic graph-only continuation remains disabled."],
    }
    output = ROOT / "evaluation/graph-cross-store-recovery-current"
    output.with_suffix(".json").write_text(json.dumps(report, indent=2) + "\n")
    output.with_suffix(".md").write_text(
        "# Graph deletion recovery — isolated PostgreSQL and Neo4j\n\n"
        f"Generated: {report['generated_at_utc']}.\n\n"
        "Command: `.venv/bin/python scripts/evaluate_graph_cross_store_recovery.py --socket-dir <private-test-socket>`. "
        "Requires a disposable local PostgreSQL cluster and an already-installed official Neo4j image.\n\n"
        f"{len(checks)}/{len(checks)} checks passed. Hosted provider calls: **0**. "
        "PostgreSQL schema, Neo4j container and network were removed.\n\n"
        + "\n".join(f"- {name}: pass" for name in checks)
        + "\n\n## Limits\n\n" + "\n".join(f"- {limit}" for limit in report["limits"])
        + "\n\nSource SHA-256 hashes and elapsed time are recorded in the adjacent JSON. "
        "Elapsed time is a test observation, not a capacity metric.\n")
    print(json.dumps({"checks_passed": len(checks), "hosted_provider_calls": 0,
        "temporary_resources_removed": True}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=55432)
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--schema", default="")
    parser.add_argument("--intent-id", default="")
    parser.add_argument("--neo4j-uri", default="")
    args = parser.parse_args()
    asyncio.run(killed_child(args) if args.child else run(args))
