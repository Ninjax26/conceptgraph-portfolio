"""Actual isolated Neo4j transaction checks; no model calls or user containers.

Creates its own labelled container/network from an already-installed
official image. Synthetic data is disposable; no host volumes or .env edits.
"""

import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from neo4j import AsyncGraphDatabase
from neo4j.exceptions import ServiceUnavailable
from app.schemas.extraction import ConceptNode, ConceptRelationship, GraphExtractionResponse
from app.services.ingestion_service import GraphProjectionMarker, IngestionService


def docker(*args):
    result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError(f"Docker test operation failed: {args[0]}: {result.stderr.strip()}")
    return result.stdout.strip()


def graph(upload="target", count=2):
    return GraphExtractionResponse(
        nodes=[ConceptNode(id=str(i), name=f"Concept {i}", type="concept", upload_id=upload,
            document_name="fixture.pdf", source_chunk_id=f"{upload}:1:{i}", page_number=1)
            for i in range(count)],
        relationships=[ConceptRelationship(source_node_id=str(i), target_node_id=str(i+1),
            relation_type="PREREQUISITE_OF") for i in range(count-1)],
    )


async def rows(driver, query, **parameters):
    async with driver.session() as session:
        result = await session.run(query, **parameters)
        return await result.data()


async def state(driver):
    # Ignore volatile course timestamps and the fence's lock counter.
    nodes = await rows(driver, "MATCH (n) RETURN labels(n) AS labels, properties(n) AS p ORDER BY n.id")
    for row in nodes:
        row["p"].pop("updated_at", None)
        row["p"].pop("lock_epoch", None)
    edges = await rows(driver, "MATCH (a)-[r]->(b) RETURN a.id AS a, b.id AS b, type(r) AS t, properties(r) AS p ORDER BY a.id, b.id, type(r)")
    return {"nodes": nodes, "edges": edges}


async def verify(driver):
    service = IngestionService(graph_driver=driver, vector_client=SimpleNamespace())
    checks = {}
    self_identity = dict(upload_id="target", document_name="fixture.pdf",
                         course_name="Fixture course", execution_token="ready-task")

    async def replace(extraction=None, *, marker=None):
        await service.replace_graph_extraction(extraction if extraction is not None else graph(), "course",
            **self_identity, marker=marker)

    async def seed():
        await rows(driver, "MATCH (n) DETACH DELETE n")  # Only this owned disposable server.
        for upload, course in (("target", "course"), ("neighbor", "course"), ("other", "other-course")):
            await service.store_graph_extraction(graph(upload), course, upload_id=upload,
                document_name="fixture.pdf", course_name="Fixture course", execution_token="ready-task")

    assert (await rows(driver, "MATCH (n) RETURN count(n) AS count"))[0]["count"] == 0
    version = (await rows(driver, "CALL dbms.components() YIELD versions RETURN versions[0] AS version"))[0]["version"]
    await seed()
    before = await state(driver)
    await replace(graph(count=13))
    result = await rows(driver, "MATCH (n:Concept {upload_id:'target'}) RETURN count(n) AS count, collect(DISTINCT n.execution_token) AS tokens")
    assert result == [{"count": 13, "tokens": ["ready-task"]}]
    assert (await rows(driver, "MATCH ()-[r:PREREQUISITE_OF {upload_id:'target'}]->() RETURN count(r) AS count"))[0]["count"] == 12
    after = await state(driver)
    for key in ("nodes", "edges"):
        protected_ids = {service._scoped_concept_id(course, upload, str(i))
            for upload, course in (("neighbor", "course"), ("other", "other-course")) for i in range(2)}
        def protected(row):
            return row["p"].get("upload_id") in {"neighbor", "other"} or row.get("b") in protected_ids
        untouched_before = [r for r in before[key] if protected(r)]
        untouched_after = [r for r in after[key] if protected(r)]
        assert untouched_before == untouched_after
    checks["merged_graph_replacement_preserves_other_uploads_and_ready_scope"] = True

    await replace(graph(count=13))
    assert await state(driver) == after
    checks["replay_preserves_graph_content_without_duplicate_edges"] = True

    await seed()
    marker = GraphProjectionMarker("intent", "job", "target", "course", "ready-task", "a" * 64, 1)
    await replace(graph(count=3), marker=marker)
    assert await rows(driver, """SHOW CONSTRAINTS YIELD name, type
        WHERE name = 'graph_projection_fence_upload'
        RETURN name, type""") == [
            {"name": "graph_projection_fence_upload", "type": "UNIQUENESS"}]
    assert await service.read_graph_publication_marker("intent") == marker
    assert await service.read_graph_publication_marker("absent") is None
    committed = await state(driver)
    await replace(graph(count=3), marker=marker)
    assert await state(driver) == committed
    checks["marker_commits_atomically_and_replay_reads_exact_identity"] = True

    await seed()
    await rows(driver, "CREATE (:GraphPublication {id:'intent', job_id:'another-job'})")
    before_conflict = await state(driver)
    try:
        await replace(graph(count=3), marker=marker)
    except ValueError:
        pass
    else:
        raise AssertionError("Conflicting remote marker identity was accepted")
    assert await state(driver) == before_conflict
    checks["marker_identity_conflict_rolls_back_projection"] = True

    await seed()
    await replace(graph(count=3), marker=marker)
    await rows(driver, "CREATE (:GraphPublication {id:'neighbor-intent', upload_id:'neighbor', course_id:'course'})")
    await rows(driver, "CREATE (:GraphPublication {id:'other-intent', upload_id:'other', course_id:'other-course'})")
    await service.cleanup_graph("target", "course")
    assert await service.read_graph_publication_marker("intent") is None
    assert (await rows(driver, "MATCH (n:Concept {upload_id:'target'}) RETURN count(n) AS count"))[0]["count"] == 0
    assert (await rows(driver, "MATCH (n:Concept {upload_id:'neighbor'}) RETURN count(n) AS count"))[0]["count"] == 2
    assert set((await rows(driver, "MATCH (n:GraphPublication) RETURN collect(n.id) AS ids"))[0]["ids"]) == {"neighbor-intent", "other-intent"}
    await service.cleanup_graph("target", "course")
    assert (await rows(driver, "MATCH (n:GraphPublication) RETURN count(n) AS count"))[0]["count"] == 2
    checks["upload_cleanup_removes_only_matching_remote_marker_and_is_idempotent"] = True

    await seed()
    await replace(graph(count=3), marker=marker)
    assert await service.cleanup_graph_publication(marker) is True
    assert await service.cleanup_graph_publication(marker) is False
    assert await service.read_graph_publication_marker("intent") is None
    assert (await rows(driver, "MATCH (n:Concept {upload_id:'target'}) RETURN count(n) AS count"))[0]["count"] == 0
    assert (await rows(driver, "MATCH (n:Concept {upload_id:'neighbor'}) RETURN count(n) AS count"))[0]["count"] == 2
    checks["exact_obsolete_publication_cleanup_is_idempotent_and_preserves_neighbors"] = True

    await seed()
    await replace(graph(count=3), marker=marker)
    newer = GraphProjectionMarker("new-intent", "new-job", "target", "course", "new-ready-task", "b" * 64, 2)
    await service.replace_graph_extraction(graph(count=4), "course", upload_id="target",
        document_name="fixture.pdf", course_name="Fixture course",
        execution_token="new-ready-task", marker=newer)
    assert await service.cleanup_graph_publication(marker) is True
    assert await service.read_graph_publication_marker("intent") is None
    assert await service.read_graph_publication_marker("new-intent") == newer
    assert (await rows(driver, "MATCH (n:Concept {upload_id:'target'}) RETURN count(n) AS count, collect(DISTINCT n.execution_token) AS tokens")) == [
        {"count": 4, "tokens": ["new-ready-task"]}]
    checks["obsolete_marker_cleanup_preserves_newer_execution_projection"] = True

    # A newer source task must win even when an exact old marker replays late.
    await seed()
    await replace(graph(count=3), marker=marker)
    await service.replace_graph_extraction(graph(count=4), "course", upload_id="target",
        document_name="fixture.pdf", course_name="Fixture course",
        execution_token="new-ready-task", marker=newer)
    try:
        await replace(graph(count=3), marker=marker)
    except ValueError:
        pass
    else:
        raise AssertionError("A stale publication replaced a newer task's graph")
    assert (await rows(driver, "MATCH (n:Concept {upload_id:'target'}) "
        "RETURN count(n) AS count, collect(DISTINCT n.execution_token) AS tokens")) == [
            {"count": 4, "tokens": ["new-ready-task"]}]
    checks["late_old_marker_replay_cannot_overwrite_newer_task_projection"] = True

    await seed()
    await replace(graph(count=3), marker=marker)
    conflicting_task = GraphProjectionMarker("same-attempt-new-task", "other-job",
        "target", "course", "other-ready-task", "c" * 64, 1)
    before_conflict = await state(driver)
    try:
        await service.replace_graph_extraction(graph(count=4), "course",
            upload_id="target", document_name="fixture.pdf",
            course_name="Fixture course", execution_token="other-ready-task",
            marker=conflicting_task)
    except ValueError:
        pass
    else:
        raise AssertionError("A different task reused an already-published generation")
    assert await state(driver) == before_conflict
    checks["same_generation_different_task_cannot_replace_projection"] = True

    changed_payload = GraphProjectionMarker("same-attempt-new-payload", "other-job",
        "target", "course", "ready-task", "d" * 64, 1)
    try:
        await replace(graph(count=4), marker=changed_payload)
    except ValueError:
        pass
    else:
        raise AssertionError("A different payload reused an already-published generation")
    assert await state(driver) == before_conflict
    checks["same_generation_different_payload_cannot_replace_projection"] = True

    for failure in ("callback", "cypher"):
        await seed()
        before = await state(driver)

        async def failing_write(tx, *args, **kwargs):
            result = await tx.run("CREATE (:Concept {id:'uncommitted', course_id:'course', upload_id:'target'})")
            await result.consume()
            if failure == "cypher":
                await tx.run("THIS IS NOT CYPHER")
            raise RuntimeError("injected callback failure")

        with patch.object(service, "_write_graph_extraction", failing_write):
            try:
                await replace()
            except Exception:
                pass
            else:
                raise AssertionError("Injected transaction failure was swallowed")
        assert await state(driver) == before
        checks[f"real_transaction_rollback_after_delete_and_{failure}_failure"] = True

    await seed()
    original_write, attempts = service._write_graph_extraction, 0

    async def retry_write(tx, *args, **kwargs):
        nonlocal attempts
        attempts += 1
        await original_write(tx, *args, **kwargs)
        if attempts == 1:
            raise ServiceUnavailable("synthetic retryable failure before commit")

    with patch.object(service, "_write_graph_extraction", retry_write):
        await replace(graph(count=3))
    assert attempts == 2
    assert (await rows(driver, "MATCH (n:Concept {upload_id:'target'}) RETURN count(n) AS count"))[0]["count"] == 3
    assert (await rows(driver, "MATCH ()-[r:PREREQUISITE_OF {upload_id:'target'}]->() RETURN count(r) AS count"))[0]["count"] == 2
    checks["actual_driver_retries_entire_callback_after_injected_retryable_failure"] = True

    await seed()
    started, release = asyncio.Event(), asyncio.Event()

    async def paused_write(tx, *args, **kwargs):
        await original_write(tx, *args, **kwargs)
        started.set()
        await release.wait()

    with patch.object(service, "_write_graph_extraction", paused_write):
        task = asyncio.create_task(replace(graph(count=3)))
        try:
            await asyncio.wait_for(started.wait(), 10)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            release.set()
            try:
                await asyncio.wait_for(task, 10)
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError("Cancellation was swallowed")
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
    assert (await rows(driver, "MATCH (n:Concept {upload_id:'target'}) RETURN count(n) AS count"))[0]["count"] == 3
    assert (await rows(driver, "MATCH ()-[r:PREREQUISITE_OF {upload_id:'target'}]->() RETURN count(r) AS count"))[0]["count"] == 2
    checks["repeated_cancellation_drains_actual_transaction_before_propagating"] = True

    await seed()
    await replace(GraphExtractionResponse(empty_extraction=True))
    assert (await rows(driver, "MATCH (n:Concept {upload_id:'target'}) RETURN count(n) AS count"))[0]["count"] == 0
    assert (await rows(driver, "MATCH (n:Course) RETURN count(n) AS count"))[0]["count"] == 2
    await service.replace_graph_extraction(GraphExtractionResponse(empty_extraction=True), "other-course",
        upload_id="other", document_name="fixture.pdf", course_name="Fixture course", execution_token="ready-task")
    assert (await rows(driver, "MATCH (n:Course) RETURN collect(n.id) AS ids"))[0]["ids"] == ["course"]
    assert (await rows(driver, "MATCH (n:Concept) RETURN collect(DISTINCT n.upload_id) AS ids"))[0]["ids"] == ["neighbor"]
    checks["empty_replacement_removes_only_target_and_only_empty_course"] = True
    return version, checks


async def main():
    token = uuid4().hex
    name, network = f"cg-projection-test-{token}", None
    container, driver = None, None
    started = time.perf_counter()
    label = f"conceptgraph.disposable-verification={token}"
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
            raise RuntimeError("Test database must bind only loopback")
        driver = AsyncGraphDatabase.driver(f"bolt://{binding}", auth=None,
            connection_timeout=2, max_transaction_retry_time=2, initial_retry_delay=0.01)
        deadline = time.monotonic() + 90
        while True:
            if docker("inspect", container, "--format", "{{.State.Running}}") != "true":
                raise RuntimeError("Disposable Neo4j exited: " + docker("logs", container)[-3000:])
            try:
                await driver.verify_connectivity()
                break
            except ServiceUnavailable:
                if time.monotonic() >= deadline:
                    raise
                await asyncio.sleep(1)
        version, checks = await verify(driver)
    finally:
        try:
            if driver is not None:
                await driver.close()
        finally:
            try:
                if container is not None:
                    # IDs come only from this run's creates, never names/globs/user input.
                    docker("stop", "--time", "10", container)
                    docker("rm", "--volumes", container)
            finally:
                if network is not None:
                    docker("network", "rm", network)
    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "kind": "isolated real Neo4j transaction checks with synthetic graph/failure fixtures",
        "neo4j_version": version, "image_id": image_id,
        "elapsed_seconds": round(time.perf_counter()-started, 3),
        "checks": checks, "passed": all(checks.values()),
        "hosted_provider_calls": 0, "production_test_data_mutations": 0,
        "disposable_container_and_network_removed": True,
        "code_sha256": {p: hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in (
            "app/core/blocking.py", "app/services/ingestion_service.py", "scripts/evaluate_graph_projection.py")},
        "limits": ["No PostgreSQL/job fence, Qdrant/R2 or full ingestion worker exercised.",
            "Failures and graph outputs are authored; retry exception is injected, not a network outage.",
            "No hard-kill, ambiguous distributed commit, semantic accuracy or load-capacity claim.",
            "The generation fence rejects a late old marked write after a newer attempt; it has no worker caller yet.",
            "Deletion does not tombstone the fence: a late marked write after deletion could recreate the graph. Do not activate continuation until deletion and reconciliation are fenced.",
            "Automatic continuation remains disabled; this verifies its internal projection primitive only."]}
    directory = ROOT / "evaluation"
    (directory / "graph-projection-current.json").write_text(json.dumps(report, indent=2)+"\n")
    table = "\n".join(f"| `{key}` | Pass |" for key in checks)
    limits = "\n".join(f"- {item}" for item in report["limits"])
    (directory / "graph-projection-current.md").write_text(
        f"# Graph projection — isolated Neo4j verification\n\nGenerated: {report['generated_at_utc']}.\n\n"
        "Command: `.venv/bin/python scripts/evaluate_graph_projection.py`. Requires running Docker and an already-installed official `neo4j:5-community` image; no image pull.\n\n"
        f"{len(checks)}/{len(checks)} checks passed against Neo4j {version}. Hosted provider calls: **0**. Production mutations: **0**. "
        "The separate-network, loopback-bound test container had no host volume, disabled usage reporting, and was removed with its data and network.\n\n"
        f"| Check | Result |\n| --- | --- |\n{table}\n\n"
        f"## Limits\n\n{limits}\n\n"
        "Image and source SHA-256 hashes are recorded in the adjacent JSON. Elapsed time is a test-run observation, not throughput or capacity.\n")
    print(json.dumps({"passed": len(checks), "neo4j_version": version, "temporary_resources_removed": True}))


if __name__ == "__main__":
    asyncio.run(main())
