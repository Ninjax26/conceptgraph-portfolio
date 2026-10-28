"""Owned local Neo4j ordering checks; no live ingestion/PG/provider calls.

The legacy unmarked path is deliberately measured separately and still blocks
activation. New safety assertions use the explicit generation-aware contract.
"""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from neo4j import AsyncGraphDatabase
from neo4j.exceptions import ServiceUnavailable
from app.schemas.extraction import GraphExtractionResponse
from app.services.ingestion_service import GraphProjectionMarker, IngestionService
from scripts.evaluate_graph_projection import docker, graph, rows, state, verify as verify_legacy


async def verify(driver):
    service = IngestionService(graph_driver=driver, vector_client=SimpleNamespace())
    checks = {}
    old = GraphProjectionMarker("old", "job-1", "target", "course", "task-1", "a"*64, 1)
    current = GraphProjectionMarker("current", "job-2", "target", "course", "task-2", "b"*64, 2)

    async def seed():
        await rows(driver, "MATCH (n) DETACH DELETE n")  # This owned test server only.
        for upload, course in (("neighbor", "course"), ("foreign", "other-course")):
            await service.store_graph_extraction(graph(upload), course, upload_id=upload,
                document_name="fixture.pdf", execution_token="neighbor-task")

    async def publish(marker=old, count=3):
        await service.replace_graph_extraction(graph(count=count), "course", upload_id="target",
            document_name="fixture.pdf", course_name="Fixture", execution_token=marker.document_task_id,
            marker=marker)

    async def baseline(generation=2, token="task-2", count=4, writer=service):
        await writer.replace_graph_extraction(graph(count=count), "course", upload_id="target",
            document_name="fixture.pdf", course_name="Fixture", execution_token=token,
            source_generation=generation)

    async def abort(generation=2, token="task-2", writer=service):
        await writer.cleanup_graph_attempt("target", "course", execution_token=token,
            source_generation=generation)

    async def count(token):
        return (await rows(driver, "MATCH(n:Concept {upload_id:'target',execution_token:$token}) "
            "RETURN count(n) AS n", token=token))[0]["n"]

    async def rejected(operation):
        before = await state(driver)
        try:
            await operation
        except ValueError:
            pass
        else:
            raise AssertionError("A stale/conflicting graph operation was accepted")
        assert await state(driver) == before

    await seed()
    await publish()
    await service.cleanup_graph("target", "course")
    await service.store_graph_extraction(graph(count=4), "course", upload_id="target",
        document_name="fixture.pdf", execution_token="task-2")
    before = await count("task-2")
    await publish()  # Exact replay after the still-unfenced legacy retry.
    legacy = {"new_task_concepts_before": before, "new_task_concepts_after": await count("task-2")}
    legacy["safe"] = legacy["new_task_concepts_before"] == legacy["new_task_concepts_after"]
    print(json.dumps({"legacy_observation": legacy}), flush=True)

    await seed()
    await publish()
    await baseline()
    await rejected(publish())
    assert await count("task-2") == 4
    assert await service.read_graph_publication_marker(old.id) is None
    checks["fenced_retry_preserves_new_graph_against_exact_older_marked_replay"] = True

    await rejected(abort(1, "task-1"))
    await rejected(abort(2, "foreign-task"))
    assert await count("task-2") == 4
    checks["older_and_same_generation_foreign_cleanup_cannot_erase_retry"] = True

    await seed()
    await baseline(1, "task-1", 2)
    await baseline(1, "task-1", 2)
    await rejected(baseline(1, "task-1", 3))
    await rejected(baseline(1, "foreign-task", 2))
    checks["baseline_replay_is_idempotent_but_payload_and_task_changes_are_rejected"] = True
    await publish()
    await rejected(baseline(1, "task-1", 2))
    await rejected(abort(1, "task-1"))
    assert await service.read_graph_publication_marker(old.id) == old
    assert await count("task-1") == 3
    checks["baseline_can_advance_to_marked_publication_but_never_reverse_or_abort_it"] = True

    await seed()
    await baseline(1, "task-1", 2)
    await abort(1, "task-1")
    await abort(1, "task-1")
    assert await count("task-1") == 0
    await rejected(baseline(1, "task-1", 2))
    await rejected(publish())
    await baseline()
    assert await count("task-2") == 4
    checks["attempt_cleanup_closes_late_writes_but_allows_a_new_generation"] = True

    await seed()
    await abort(1, "task-1")
    await rejected(baseline(1, "task-1", 2))
    await rejected(publish())
    await baseline()
    checks["cleanup_before_first_projection_closes_the_failed_generation"] = True

    await service.tombstone_graph_publication("target", "course")
    await rejected(baseline(3, "task-3", 2))
    later = GraphProjectionMarker("later", "job-3", "target", "course", "task-3", "c"*64, 3)
    await rejected(publish(later))
    await rejected(abort(3, "task-3"))
    checks["permanent_deletion_rejects_even_higher_baseline_marked_and_cleanup_generations"] = True

    async def overlap(first, second, paused_token):
        entered, release = asyncio.Event(), asyncio.Event()
        original = service._write_graph_extraction
        async def held_write(tx, *args, **kwargs):
            if kwargs["execution_token"] == paused_token:
                entered.set()
                await release.wait()
            await original(tx, *args, **kwargs)
        tasks = []
        with patch.object(service, "_write_graph_extraction", held_write):
            try:
                tasks.append(asyncio.create_task(first()))
                await asyncio.wait_for(entered.wait(), 10)
                tasks.append(asyncio.create_task(second()))
                await asyncio.sleep(0.1)
                assert not tasks[1].done()
                release.set()
                return await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 15)
            finally:
                release.set()
                await asyncio.gather(*tasks, return_exceptions=True)

    await seed()
    results = await overlap(publish, baseline, "task-1")
    assert results == [None, None] and await count("task-2") == 4
    checks["new_baseline_waits_for_inflight_older_publication_then_wins"] = True

    await seed()
    results = await overlap(baseline, publish, "task-2")
    assert results[0] is None and isinstance(results[1], ValueError)
    assert await count("task-2") == 4
    checks["late_publication_waits_for_inflight_new_baseline_then_is_rejected"] = True

    await seed()
    results = await overlap(baseline, abort, "task-2")
    assert results == [None, None] and await count("task-2") == 0
    await rejected(baseline())
    checks["attempt_cleanup_waits_for_same_generation_baseline_then_closes_it"] = True

    await seed()
    await baseline(1, "task-1", 2)
    snapshot = await state(driver)
    async def broken_write(tx, *args, **kwargs):
        await tx.run("CREATE (:Concept {id:'uncommitted', upload_id:'target'})")
        raise RuntimeError("synthetic failure after fence/delete")
    with patch.object(service, "_write_graph_extraction", broken_write):
        try:
            await baseline()
        except RuntimeError:
            pass
        else:
            raise AssertionError("Injected baseline failure was swallowed")
    assert await state(driver) == snapshot
    checks["failed_baseline_rolls_back_projection_and_generation_together"] = True

    class BrokenCleanupDriver:
        @asynccontextmanager
        async def session(self):
            async with driver.session() as actual:
                async def execute_write(callback):
                    async def wrapped(tx):
                        async def run(query, **parameters):
                            if "DETACH DELETE marker" in query:
                                raise RuntimeError("synthetic failure after concept deletion")
                            return await tx.run(query, **parameters)
                        return await callback(SimpleNamespace(run=run))
                    return await actual.execute_write(wrapped)
                yield SimpleNamespace(run=actual.run, execute_write=execute_write)

    broken = IngestionService(graph_driver=BrokenCleanupDriver(), vector_client=SimpleNamespace())
    try:
        await abort(writer=broken)
    except RuntimeError:
        pass
    else:
        raise AssertionError("Injected cleanup failure was swallowed")
    assert await state(driver) == snapshot
    checks["failed_cleanup_rolls_back_concept_deletion_and_aborted_generation"] = True

    await service.replace_graph_extraction(GraphExtractionResponse(), "course", upload_id="target",
        document_name="fixture.pdf", course_name="Fixture", execution_token="task-2", source_generation=2)
    await rejected(publish())
    await rejected(abort(1, "task-1"))
    assert await count("task-2") == 0
    checks["empty_baseline_still_advances_fence_without_leaving_concepts"] = True

    assert (await rows(driver, "MATCH(n:Concept {upload_id:'neighbor'}) RETURN count(n) AS n"))[0]["n"] == 2
    assert (await rows(driver, "MATCH(n:Concept {upload_id:'foreign'}) RETURN count(n) AS n"))[0]["n"] == 2
    checks["other_upload_and_other_course_survive_generation_writes_and_cleanup"] = True
    return checks, legacy


async def main():
    token = uuid4().hex
    label, name = f"conceptgraph.disposable-baseline={token}", f"cg-baseline-{token}"
    container = network = driver = None
    try:
        image_id = docker("image", "inspect", "neo4j:5-community", "--format", "{{.Id}}")
        network = docker("network", "create", "--label", label, name)
        container = docker("run", "--detach", "--pull=never", "--name", name,
            "--label", label, "--network", network, "--publish", "127.0.0.1::7687",
            "--memory", "1g", "--cpus", "1", "--tmpfs", "/data:rw,size=256m",
            "--env", "NEO4J_AUTH=none", "--env", "NEO4J_server_memory_heap_initial__size=256m",
            "--env", "NEO4J_server_memory_heap_max__size=256m",
            "--env", "NEO4J_server_memory_pagecache_size=64m",
            "--env", "NEO4J_dbms_usage__report_enabled=false",
            "--env", "NEO4J_db_tx__log_preallocate=false",
            "--env", "NEO4J_db_tx__log_rotation_size=16M", image_id)
        binding = docker("port", container, "7687/tcp")
        if not binding.startswith("127.0.0.1:") or "\n" in binding:
            raise RuntimeError("Only the owned loopback fixture is allowed")
        driver = AsyncGraphDatabase.driver(f"bolt://{binding}", auth=None, connection_timeout=2,
            max_transaction_retry_time=2, initial_retry_delay=0.01)
        deadline = asyncio.get_running_loop().time() + 90
        while True:
            try:
                await driver.verify_connectivity()
                break
            except ServiceUnavailable:
                if asyncio.get_running_loop().time() >= deadline:
                    raise
                await asyncio.sleep(1)
        version, legacy_checks = await verify_legacy(driver)
        checks, legacy = await verify(driver)
    finally:
        try:
            if driver is not None:
                await driver.close()
        finally:
            try:
                if container is not None:
                    docker("rm", "--force", "--volumes", container)
            finally:
                if network is not None:
                    docker("network", "rm", network)
    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(), "neo4j_version": version,
        "image_id": image_id, "checks": checks, "legacy_projection_regressions": legacy_checks,
        "checks_passed": len(checks), "legacy_checks_passed": len(legacy_checks),
        "legacy_unfenced_ordering": legacy, "runtime_integration_enabled": False,
        "phase3_complete": False, "disposable_resources_removed": True,
        "hosted_calls": 0, "production_test_data_mutations": 0,
        "source_sha256": {name: hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in (
            "app/services/ingestion_service.py", "app/core/blocking.py",
            "scripts/evaluate_graph_projection.py", "scripts/evaluate_graph_baseline_projection.py",
            "tests/test_graph_baseline_projection.py")},
        "limits": [
            "No PostgreSQL source/job authority, actual processor, Qdrant, R2 or HTTP API is exercised.",
            "Legacy ordinary writes/cleanup remain unchanged and unsafe in the separate replay probe.",
            "New checks use explicit source generations; live integration and cleanup qualification are still required.",
            "Outputs/failures are synthetic; no new hard-kill, capacity, semantic-quality or provider measurements.",
            "The existing projection regressions include real rollback, managed retry and cancellation checks.",
            "Automatic continuation remains disabled; this is not end-to-end retry or deletion acceptance.",
        ],
    }
    output = ROOT/"evaluation/graph-baseline-projection-current"
    output.with_suffix(".json").write_text(json.dumps(report, indent=2)+"\n")
    output.with_suffix(".md").write_text("# Generation-compatible graph projections — isolated Neo4j\n\n"
        "Command: `.venv/bin/python scripts/evaluate_graph_baseline_projection.py`.\n\n"
        f"{len(checks)} new ordering checks and {len(legacy_checks)} existing projection regressions passed. "
        f"Legacy unfenced ordering safe: **{legacy['safe']}**. Live integration remains disabled.\n\n"
        + "\n".join(f"- {name}: passed" for name in checks)
        + "\n\nLimits:\n\n" + "\n".join(f"- {item}" for item in report["limits"])+"\n")
    print(json.dumps({"new_checks": len(checks), "legacy_checks": len(legacy_checks),
        "legacy_ordering_safe": legacy["safe"], "temporary_resources_removed": True}))


if __name__ == "__main__":
    asyncio.run(main())
