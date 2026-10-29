"""Known-cohort transition rehearsal. Real entry points/stores; offline AI only."""
import asyncio
import json
import os
from pathlib import Path
import signal
import socket
import sys
import tempfile
from uuid import UUID, uuid4

import httpx
import pymupdf
from qdrant_client import models
from sqlalchemy import select

from app.core.blocking import run_blocking_safely
from app.models.document_upload import DocumentUpload, ProcessingAttempt, VectorDeletionTombstone
from app.services.resource_budget_service import ResourceBudgetService
from app.services.upload_service import UploadService
from app.services.rollout_inventory import compare_inventories
from app.services.vector_rollout_audit import (ReadyVectorIdentity, VectorReadContract,
    audit_ready_vectors, legacy_read_snapshot_compatible)
from scripts.deferred_vector_proxy import DeferredVectorProxy
from scripts.processor_crash_checks import AuthoredIngestion
from scripts.worker_lifecycle_checks import lifecycle_config


async def verify_drain_rollback(context, sessions, driver, vector, storage):
    from scripts.evaluate_upload_deletion import TOKEN
    checks, observations, processes, clients, stopped = {}, {}, [], [], set()
    headers = {"Authorization": "Bearer "+TOKEN}
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_textbox((60, 70, 530, 500), "DNS Resolution\n"
            "DNS translates a domain name into an IP address before a browser sends packets. "
            "TCP establishes a connection and TLS protects the connection. This authored native "
            "PDF tests isolated process transitions; it does not measure answer quality.", fontsize=12)
        content = pdf.tobytes()

    async def record(upload):
        async with sessions() as session:
            return await session.get(DocumentUpload, upload)

    async def wait_until(predicate, label):
        async with asyncio.timeout(35):
            while True:
                for process in processes:
                    if process.returncode is not None and process not in stopped:
                        output, _ = await process.communicate()
                        raise AssertionError(f"Unexpected {process.returncode} during {label}: "+output.decode()[-2000:])
                value = await predicate()
                if value:
                    return value
                await asyncio.sleep(.05)

    async def ready_document(upload):
        async def ready():
            row = await record(upload)
            assert row.status != "failed", row.error_message
            return row if row.status == "ready" else None
        return await wait_until(ready, "document READY")

    async def finish(process, expected):
        stopped.add(process)
        output, _ = await asyncio.wait_for(process.communicate(), 20)
        assert process.returncode == expected, output.decode()[-2000:]

    async def stop_worker(process, manifest):
        process.send_signal(signal.SIGTERM)
        await finish(process, 0)
        assert manifest.with_suffix(".worker.stop-completed").read_text() == "stop.completed"

    async def stop_api(process, manifest):
        process.send_signal(signal.SIGTERM)
        await finish(process, -signal.SIGTERM)
        assert (manifest.parent/"api-lifespan-closed").read_text() == "lifespan.shutdown.complete"

    async def points(upload):
        found, cursor = await asyncio.to_thread(vector.scroll, context["collection"],
            scroll_filter=models.Filter(must=[models.FieldCondition(key="upload_id",
                match=models.MatchValue(value=upload))]), limit=100, with_vectors=True)
        assert cursor is None, "The small fixture must not silently truncate a snapshot."
        return found

    async def source_state(upload, *, exists):
        row = await record(upload)
        async with driver.session() as session:
            result = await session.run("MATCH (n:Concept {upload_id:$upload}) RETURN count(n) AS n", upload=upload)
            count = (await result.single())["n"]
        vectors = await points(upload)
        if exists:
            assert row.status == "ready" and count == row.graph_node_count > 0
            assert len(vectors) == row.processed_chunk_count > 0
            assert all(p.payload["execution_token"] == row.task_id for p in vectors)
            assert await asyncio.to_thread(storage.exists, row.storage_key)
        else:
            assert row is None and count == 0 and not vectors
        return row

    async def upload(client, label):
        response = await client.post("/api/v1/ingest/upload", headers=headers,
            data={"course_id": "Drain "+label+" "+uuid4().hex},
            files={"file": ("transition.pdf", content, "application/pdf")})
        assert response.status_code == 202, response.text
        return response.json()

    async def query_preview(client, source):
        response = await client.post("/api/v1/query", headers=headers,
            json={"course_id": source["course_id"], "question": "What does DNS translate?",
                  "retrieval_mode": "vector_only"})
        assert response.status_code == 200, response.text
        answer = response.json()
        assert answer["sources"] and all(s["document_id"] == source["upload_id"] for s in answer["sources"])
        assert answer["generation_metadata"]["provider_used"] == "evidence_fallback"
        assert "[Source 1]" in answer["answer"]
        response = await client.get(f'/api/v1/ingest/uploads/{source["upload_id"]}/preview',
            headers={**headers, "Range": "bytes=0-19"})
        assert response.status_code == 206 and response.content.startswith(b"%PDF")

    async def obligations(upload):
        async with sessions() as session:
            rows = (await session.execute(select(VectorDeletionTombstone).where(
                VectorDeletionTombstone.upload_id == upload))).scalars().all()
            return {(r.upload_id, r.task_id, r.store_sha256) for r in rows}

    async def audit(upload):
        async with sessions() as session:
            row = await session.get(DocumentUpload, upload)
            tasks = set((await session.execute(select(ProcessingAttempt.task_id).where(
                ProcessingAttempt.document_id == upload))).scalars()) | {row.task_id}
            identity = ReadyVectorIdentity.from_record(row, known_tasks=tasks)
        contract = VectorReadContract.from_settings(lifecycle_config(storage.config, "worker"))
        result = await audit_ready_vectors(vector, contract, identity, peer_contract=contract)
        assert not result["activation_allowed"] and not result["writer_drain_verified"]
        return result

    with tempfile.TemporaryDirectory(prefix="cg-drain-rollback-", dir="/private/tmp") as directory:
        async def launch(role, name, *, scoped, transport=None):
            folder = Path(directory)/name
            folder.mkdir(mode=0o700, exist_ok=True)
            manifest = folder/("transport.json" if transport else "context.json")
            manifest.write_text(json.dumps({**context, "cohort_scoped":scoped,
                "fleet_inventory":True, "drain_rehearsal":True,
                **({"vector_transport":transport} if transport else {})}))
            manifest.chmod(0o600)
            command = [sys.executable, str(Path(__file__).with_name("worker_lifecycle_checks.py")),
                "--context", str(manifest), "--role", role]
            listener = None
            try:
                if role == "api":
                    listener = socket.socket()
                    listener.bind(("127.0.0.1", 0))
                    listener.listen()
                    command += ["--listener-fd", str(listener.fileno())]
                process = await asyncio.create_subprocess_exec(*command, cwd="/private/tmp",
                    pass_fds=(listener.fileno(),) if listener else (), env={**os.environ, "PYTHONDONTWRITEBYTECODE":"1"},
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
                processes.append(process)
                if role == "worker":
                    return process, manifest
                client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{listener.getsockname()[1]}",
                    trust_env=False, timeout=5)
                clients.append(client)
            finally:
                if listener:
                    listener.close()
            async def listening():
                try:
                    response = await client.get("/api/v1/ready")
                    return response.json() if response.status_code == 200 else None
                except httpx.HTTPError:
                    return None
            status = await wait_until(listening, "API startup")
            assert status["processing_role"] == "api"
            return process, manifest, client

        def cohort_inventory(api, api_path, worker, worker_path, *, scoped):
            manifests = [json.loads(path.with_suffix(f".{role}.inventory.json").read_text())
                for role, path in (("api",api_path),("worker",worker_path))]
            assert [m["pid"] for m in manifests] == [api.pid, worker.pid]
            assert all(m["scoped"] is scoped for m in manifests)
            comparison = compare_inventories(manifests)
            assert comparison["supplied_instances_compatible"], comparison
            assert not comparison["activation_allowed"] and not comparison["writer_drain_verified"]
            return comparison

        try:
            api, api_path, client = await launch("api", "legacy", scoped=False)
            first = await upload(client, "original")
            initial_worker, initial_path = await launch("worker", "legacy", scoped=False)
            original = await ready_document(first["upload_id"])
            original_ids = {str(p.id) for p in await points(original.upload_id)}
            assert all(UUID(value).version == 5 for value in original_ids)
            await query_preview(client, first)
            await stop_worker(initial_worker, initial_path)
            checks["legacy_ready_source_and_actual_idle_worker_shutdown"] = True

            second = await upload(client, "interrupted")
            async with DeferredVectorProxy(context["qdrant"], context["collection"],
                    second["upload_id"], second["task_id"]) as proxy:
                worker, worker_path = await launch("worker", "legacy", scoped=False, transport=proxy.url)
                captured = await asyncio.wait_for(asyncio.shield(proxy.captured), 30)
                observations["legacy_cohort_inventory"] = cohort_inventory(api, api_path, worker, worker_path, scoped=False)
                await stop_api(api, api_path)
                worker.send_signal(signal.SIGTERM)
                async def stop_entered():
                    return worker_path.with_suffix(".worker.stop-started").exists()
                await wait_until(stop_entered, "real worker stop entry")
                interrupted = await record(second["upload_id"])
                assert worker.returncode is None and not proxy.delivered.done()
                assert interrupted.lease_owner and interrupted.lease_expires_at
                assert not worker_path.with_suffix(".worker.stop-completed").exists()
                checks["transition_blocked_while_known_worker_rpc_and_lease_remain"] = True
                delivered = await proxy.release()
                assert delivered["completed"] and delivered["http_status"] == 200
                assert delivered["body_sha256"] == captured["body_sha256"]
                await finish(worker, 0)
                assert worker_path.with_suffix(".worker.stop-completed").exists()
                interrupted = await record(second["upload_id"])
                assert interrupted.status == "active" and interrupted.attempt_count == 1
                assert interrupted.task_id == second["task_id"] and interrupted.lease_owner is None
                assert interrupted.lease_expires_at is None and not proxy.rejections
                stale = await points(second["upload_id"])
                assert len(stale) == captured["point_count"] > 0
                observations["drained_request"] = delivered
            assert all(p.returncode is not None for p in processes)
            checks["all_known_legacy_actors_exit_after_exact_rpc_acknowledgement"] = True

            api, api_path, client = await launch("api", "scoped", scoped=True)
            await query_preview(client, first)
            worker, worker_path = await launch("worker", "scoped", scoped=True)
            current = await ready_document(second["upload_id"])
            assert current.attempt_count == 2 and current.task_id != second["task_id"]
            observations["scoped_cohort_inventory"] = cohort_inventory(api, api_path, worker, worker_path, scoped=True)
            assert {str(p.id) for p in await points(original.upload_id)} == original_ids
            assert (await record(original.upload_id)).task_id == original.task_id
            await query_preview(client, first)
            await query_preview(client, second)
            await source_state(original.upload_id, exists=True)
            await source_state(current.upload_id, exists=True)
            checks["scoped_reads_preserve_legacy_ready_task_and_physical_ids"] = True
            checks["released_interrupted_lease_recovers_without_fixture_clock_changes"] = True

            third = await upload(client, "deleted")
            deleted = await ready_document(third["upload_id"])
            response = await client.delete(f'/api/v1/ingest/uploads/{deleted.upload_id}', headers=headers)
            assert response.status_code == 204, response.text
            await source_state(deleted.upload_id, exists=False)
            assert not await asyncio.to_thread(storage.exists, deleted.storage_key)
            retained = await obligations(deleted.upload_id)
            assert retained
            checks["scoped_deletion_creates_real_durable_cleanup_obligations"] = True
            await stop_api(api, api_path)
            await stop_worker(worker, worker_path)
            assert all(p.returncode is not None for p in processes)
            checks["all_known_scoped_actors_drained_before_rollback_preflight"] = True

            # A deliberate controller replay, not a second late request or automatic recovery.
            await asyncio.to_thread(vector.upsert, context["collection"], points=[
                models.PointStruct(id=p.id, vector=p.vector, payload=p.payload) for p in stale], wait=True)
            dirty = await audit(current.upload_id)
            assert dirty["read_coverage_ok"] and dirty["cleanup_payloads_trackable"], dirty
            assert dirty["counts"]["known_stale_points"] == len(stale)
            assert not legacy_read_snapshot_compatible(dirty)
            checks["known_stale_payloads_block_broad_read_rollback_snapshot"] = True
            ingestion = AuthoredIngestion(driver, vector, context["collection"])
            async with sessions() as session:
                await ResourceBudgetService().lock(session)
                row = (await session.execute(select(DocumentUpload).where(
                    DocumentUpload.upload_id == current.upload_id).with_for_update())).scalar_one()
                known = set((await session.execute(select(ProcessingAttempt.task_id).where(
                    ProcessingAttempt.document_id == row.upload_id))).scalars())
                assert row.status == "ready" and row.task_id == current.task_id and not UploadService.is_deleting(row)
                assert second["task_id"] in known and second["task_id"] != row.task_id
                await run_blocking_safely(ingestion.cleanup_vectors, row.upload_id,
                    execution_token=second["task_id"])
                await session.commit()
            clean = await audit(current.upload_id)
            assert legacy_read_snapshot_compatible(clean), clean
            first_audit = await audit(original.upload_id)
            assert legacy_read_snapshot_compatible(first_audit), first_audit
            assert await obligations(deleted.upload_id) == retained
            observations["rollback_preflight_before_controller_reconciliation"] = dirty
            observations["rollback_preflight_after_controller_reconciliation"] = clean
            observations["preserved_legacy_source_preflight"] = first_audit
            checks["explicit_known_execution_cleanup_preserves_current_source_and_obligations"] = True

            api, api_path, client = await launch("api", "rollback", scoped=False)
            worker, worker_path = await launch("worker", "rollback", scoped=False)
            async def worker_present():
                return (await client.get("/api/v1/ready")).json().get("active_workers") == 1
            await wait_until(worker_present, "rollback worker presence")
            observations["rollback_cohort_inventory"] = cohort_inventory(api, api_path, worker, worker_path, scoped=False)
            await query_preview(client, first)
            await query_preview(client, second)
            for row in (original, current):
                assert (await source_state(row.upload_id, exists=True)).task_id == row.task_id
            assert {str(p.id) for p in await points(original.upload_id)} == original_ids
            assert await obligations(deleted.upload_id) == retained
            assert (await client.get(f'/api/v1/ingest/uploads/{deleted.upload_id}/preview', headers=headers)).status_code == 404
            checks["rollback_preserves_ready_tasks_queries_previews_and_cleanup_obligations"] = True
            for row in (original, current):
                response = await client.delete(f'/api/v1/ingest/uploads/{row.upload_id}', headers=headers)
                assert response.status_code == 204, response.text
                await source_state(row.upload_id, exists=False)
                assert not await asyncio.to_thread(storage.exists, row.storage_key)
            assert await obligations(deleted.upload_id) == retained
            checks["legacy_rollback_deletion_removes_owned_cross_store_data"] = True
            await stop_api(api, api_path)
            await stop_worker(worker, worker_path)
            assert all(p.returncode is not None for p in processes)
            observations.update(known_api_processes=3, known_worker_processes=4,
                known_api_exit_codes=[-signal.SIGTERM]*3, known_worker_exit_codes=[0]*4,
                fixture_lease_clock_changes=0, forced_terminations_on_success=0,
                cleanup_obligations_retained=len(retained), historical_binaries_tested=False,
                unknown_writers_discovered=False, activation_allowed=False,
                in_flight_put_count=1, stale_replay_origin="explicit fixture controller",
                drain_observer="receipts around awaited real coordinator.stop; no stop/timer logic replaced")
            return {"checks":checks, "observations":observations}
        finally:
            for client in clients:
                await client.aclose()
            for process in processes:
                if process.returncode is None:
                    process.kill()
                await process.communicate()
