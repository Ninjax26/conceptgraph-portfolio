"""Disposable PostgreSQL/Qdrant/Neo4j/MinIO deletion verification; no model calls."""
import argparse
import asyncio
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
import os
import signal
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import httpx
from fastapi import FastAPI
from neo4j import AsyncGraphDatabase
from qdrant_client import QdrantClient, models
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker
from app.api.endpoints import ingest
from app.core import database, security
from app.core.config import Settings
from app.models.document_upload import Course, DocumentUpload, GraphExtractionCheckpoint, GraphPublicationCleanupReceipt, GraphPublicationIntent
from app.services.course_service import CourseService, CourseNotReadyError
from app.services.demo_retention_service import DemoRetentionService
from app.services.ingestion_service import GraphProjectionMarker, IngestionService
from app.services.security_service import DemoAccessService, RateLimitService
from app.services.storage_service import ObjectStorageError, StorageService
from app.services.upload_service import UploadService
from scripts.evaluate_graph_projection import docker, graph, rows
from scripts.evaluate_graph_retry import fixture
from scripts.evaluate_provider_admission import engine_for

TOKEN = "isolated-deletion-fixture-token-only"


def local_url(value, scheme):
    from urllib.parse import urlsplit
    parsed = urlsplit(value)
    if parsed.scheme != scheme or parsed.hostname != "127.0.0.1" or not parsed.port:
        raise ValueError("Only loopback test endpoints are allowed")
    return value


def config(context):
    return Settings(_env_file=None, REQUIRE_UPLOAD_AUTH=True, DEMO_ACCESS_TOKEN=TOKEN,
        OBJECT_STORAGE_BACKEND="s3", S3_ENDPOINT_URL=local_url(context["s3"], "http"),
        S3_BUCKET=context["bucket"], S3_ACCESS_KEY_ID="local-test-only",
        S3_SECRET_ACCESS_KEY="local-test-only-password", S3_REGION="us-east-1",
        S3_FORCE_PATH_STYLE=True, S3_AUTO_CREATE_BUCKET=True, S3_SERVER_SIDE_ENCRYPTION=None,
        QDRANT_URL=local_url(context["qdrant"], "http"), QDRANT_COLLECTION_NAME=context["collection"],
        GRAPH_PROJECTION_FENCING_ENABLED=context.get("projection_fencing", False),
        PUBLIC_SAMPLE_COURSE_ID="", LEGACY_UPLOAD_DIR="/private/tmp/cg-no-legacy-deletion-fixtures")


def clients(context):
    driver = AsyncGraphDatabase.driver(local_url(context["neo4j"], "bolt"), auth=None, connection_timeout=2)
    vector = QdrantClient(url=local_url(context["qdrant"], "http"), timeout=5,
                          check_compatibility=False, cloud_inference=False)
    return driver, vector, StorageService(config(context))


class InjectedIngestion(IngestionService):
    def __init__(self, driver, vector, context, boundary="", failure="kill"):
        super().__init__(graph_driver=driver, vector_client=vector)
        self.collection_name = context["collection"]
        self.boundary, self.failure = boundary, failure

    def hit(self, boundary):
        if self.boundary == boundary:
            if self.failure == "kill":
                os.kill(os.getpid(), signal.SIGKILL)
            raise RuntimeError("isolated deletion interruption: " + boundary)

    def cleanup_vectors(self, upload_id, *, execution_token=None):
        super().cleanup_vectors(upload_id, execution_token=execution_token)
        self.hit("vectors")

    async def cleanup_graph(self, upload_id, course_id):
        await super().cleanup_graph(upload_id, course_id)
        self.hit("graph")

    async def tombstone_graph_publication(self, upload_id, course_id):
        await super().tombstone_graph_publication(upload_id, course_id)
        self.hit("graph")


class InjectedUploads(UploadService):
    def __init__(self, injector):
        self.injector = injector

    async def lock_for_deletion(self, session, upload_id, **kwargs):
        if self.injector.boundary == "before_intent_commit":
            event.listen(session.sync_session, "before_commit",
                         lambda session: self.injector.hit("before_intent_commit"), once=True)
        result = await super().lock_for_deletion(session, upload_id, **kwargs)
        self.injector.hit("intent")
        return result

    async def delete_document(self, session, upload_id):
        if self.injector.boundary == "before_metadata_commit":
            event.listen(session.sync_session, "before_commit",
                         lambda session: self.injector.hit("before_metadata_commit"), once=True)
        result = await super().delete_document(session, upload_id)
        self.injector.hit("metadata")
        return result


async def request(sessions, storage, service, upload_id, *, authenticated=True, operation="delete"):
    app = FastAPI()
    app.include_router(ingest.router)
    app.add_middleware(security.DemoProtectionMiddleware)
    async def db_session():
        async with sessions() as session:
            yield session
    app.dependency_overrides[database.get_postgres_session] = db_session
    with ExitStack() as stack:
        stack.enter_context(patch.object(ingest, "upload_service", InjectedUploads(service)))
        stack.enter_context(patch.object(ingest, "storage_service", storage))
        stack.enter_context(patch.object(ingest, "settings", storage.config))
        stack.enter_context(patch.object(ingest, "document_processing_service", SimpleNamespace(ingestion_service=service)))
        stack.enter_context(patch.object(security, "demo_access_service", DemoAccessService(storage.config)))
        stack.enter_context(patch.object(security, "rate_limit_service", RateLimitService()))
        stack.enter_context(patch.object(security, "settings", storage.config))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                                     base_url="http://local-test") as client:
            headers = {"Authorization": f"Bearer {TOKEN}"} if authenticated else {}
            if isinstance(upload_id, list):
                return await asyncio.gather(*(client.delete(f"/api/v1/ingest/uploads/{item}", headers=headers)
                                               for item in upload_id))
            method = {"delete":"DELETE", "retry":"POST", "preview":"GET"}[operation]
            suffix = "" if operation == "delete" else "/" + operation
            return await client.request(method, f"/api/v1/ingest/uploads/{upload_id}{suffix}", headers=headers)


async def child(args):
    context = json.loads(args.context.read_text())
    engine = engine_for(SimpleNamespace(socket_dir=Path(context["socket_dir"]), port=context["port"]), context["schema"])
    sessions = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    driver, vector, storage = clients(context)
    service = InjectedIngestion(driver, vector, context, args.boundary)
    if args.boundary == "storage":
        original = storage.delete
        def delete(key):
            original(key)
            service.hit("storage")
        storage.delete = delete
    try:
        if args.recover_child:
            with patch.object(database, "postgres_engine", engine):
                await database.initialize_database_schema()
            retention = DemoRetentionService(config=storage.config)
            retention.ingestion_service, retention.storage = service, storage
            with patch("app.services.demo_retention_service.AsyncSessionLocal", sessions):
                removed = await retention.resume_pending_deletions_once()
                receipts = await retention.cleanup_deleted_graph_publications_once()
                vectors_checked = await retention.cleanup_deleted_vectors_once()
            print(json.dumps({"removed":removed, "receipts":receipts, "vectors_checked":vectors_checked}), flush=True)
        else:
            response = await request(sessions, storage, service, args.upload_id)
            raise AssertionError(f"Hard-kill boundary not reached: {response.status_code}")
    finally:
        await driver.close()
        vector.close()
        await engine.dispose()


async def run(args):
    started, token = time.perf_counter(), uuid4().hex
    schema, name = "cg_admission_" + token, "cg-deletion-test-" + token
    admin, engine = engine_for(args), None
    driver = vector = None
    network, containers, images = None, [], {}
    schema_created, checks, observations = False, {}, {}
    worker_result = {"checks": {}, "observations": {}, "activation_safe": True}
    vector_cleanup_checks = {}
    lifecycle_result = {"checks": {}, "observations": {}}
    context = {"socket_dir":str(args.socket_dir), "port":args.port, "schema":schema,
               "collection":"deletion_"+token, "bucket":"deletion-"+token,
               "projection_fencing": args.projection_fencing}
    try:
        network = docker("network", "create", "--label", "conceptgraph.disposable-deletion="+token, name)
        specs = (
            ("neo4j", "neo4j:5-community", "7687", ["--memory","1g","--env","NEO4J_AUTH=none",
                "--env","NEO4J_server_memory_heap_initial__size=256m","--env","NEO4J_server_memory_heap_max__size=256m",
                "--env","NEO4J_server_memory_pagecache_size=64m","--env","NEO4J_dbms_usage__report_enabled=false",
                "--env","NEO4J_db_tx__log_preallocate=false","--tmpfs","/data:rw,size=256m"], []),
            ("qdrant", "qdrant/qdrant:latest", "6333", ["--memory","512m","--env","QDRANT__TELEMETRY_DISABLED=true",
                "--tmpfs","/qdrant/storage:rw,size=128m"], []),
            ("s3", "minio/minio:latest", "9000", ["--memory","384m","--env","MINIO_ROOT_USER=local-test-only",
                "--env","MINIO_ROOT_PASSWORD=local-test-only-password","--tmpfs","/data:rw,size=128m"], ["server","/data"]),
        )
        for key, image, port, options, command in specs:
            images[key] = docker("image", "inspect", image, "--format", "{{.Id}}")
            container = docker("run","--detach","--pull=never","--name",name+"-"+key,
                "--label","conceptgraph.disposable-deletion="+token,"--network",network,
                "--publish","127.0.0.1::"+port,*options,images[key],*command)
            containers.append(container)
            binding = docker("port",container,port+"/tcp")
            if not binding.startswith("127.0.0.1:") or "\n" in binding:
                raise RuntimeError("Test service must bind only loopback")
            context[key] = ("bolt://" if key == "neo4j" else "http://")+binding
        driver, vector, storage = clients(context)
        deadline = time.monotonic()+90
        while True:
            try:
                await driver.verify_connectivity()
                await asyncio.to_thread(vector.get_collections)
                await asyncio.to_thread(storage.check_ready)
                break
            except Exception:
                if time.monotonic() >= deadline:
                    raise
                await asyncio.sleep(1)
        vector.create_collection(context["collection"],vectors_config=models.VectorParams(size=3,distance=models.Distance.COSINE))
        async with admin.begin() as connection:
            postgres_version = (await connection.execute(text("SHOW server_version"))).scalar_one()
            await connection.execute(text(f"CREATE SCHEMA {schema}"))
            schema_created = True
        engine = engine_for(args,schema)
        sessions = async_sessionmaker(engine,expire_on_commit=False,autoflush=False)
        with patch.object(database,"postgres_engine",engine):
            await database.initialize_database_schema()
        service = InjectedIngestion(driver,vector,context)
        processor_checks = {}
        if args.projection_fencing:
            if args.worker_crashes:
                from scripts.vector_cleanup_checks import verify_vector_cleanup
                vector_cleanup_checks = await verify_vector_cleanup(
                    engine, sessions, driver, vector, storage, context["collection"])
            from scripts.processor_fencing_checks import verify_processor
            async def remove_processor_fixture(upload_id):
                return await request(sessions, storage, service, upload_id)
            processor_checks = await verify_processor(sessions, driver, vector, storage,
                context["collection"], remove_processor_fixture)
            if args.worker_crashes:
                from scripts.processor_crash_checks import verify_worker_crashes
                worker_result = await verify_worker_crashes(context, sessions, driver, vector, storage,
                    remove_processor_fixture)
                if args.full_lifecycle:
                    from scripts.worker_lifecycle_checks import verify_lifecycle
                    lifecycle_result = await verify_lifecycle(context, sessions, driver, vector, storage)

        async def seed(*,course=None,key=None):
            if course is None:
                course = Course(id=str(uuid4()),normalized_name="deletion fixture "+uuid4().hex,display_name="Deletion fixture")
                async with sessions() as session:
                    session.add(course)
                    await session.commit()
            upload_id,task_id,_ = await fixture(sessions,course,delay=None)
            digest = key.rsplit("/",1)[-1].removesuffix(".pdf") if key else hashlib.sha256(upload_id.encode()).hexdigest()
            key = key or StorageService.object_key(course.id,digest)
            intent_id,job_id = str(uuid4()),str(uuid4())
            async with sessions() as session:
                record = await session.get(DocumentUpload,upload_id)
                record.storage_key,record.content_hash = key,digest
                session.add(GraphPublicationIntent(id=intent_id,job_id=job_id,upload_id=upload_id,
                    document_task_id=task_id,course_id=course.id,payload_sha256="a"*64,source_generation=1))
                await session.commit()
            await asyncio.to_thread(storage.put_pdf,key,b"%PDF-1.4 isolated synthetic deletion fixture")
            vector.upsert(context["collection"],points=[models.PointStruct(id=str(uuid4()),vector=[1.,0.,0.],
                payload={"upload_id":upload_id,"course_id":course.id,"document_task_id":task_id})],wait=True)
            await service.replace_graph_extraction(graph(upload_id),course.id,upload_id=upload_id,
                document_name="fixture.pdf",course_name=course.display_name,execution_token=task_id,
                marker=GraphProjectionMarker(intent_id,job_id,upload_id,course.id,task_id,"a"*64,1))
            return SimpleNamespace(upload_id=upload_id,course=course,key=key,intent_id=intent_id)

        async def state(item):
            async with sessions() as session:
                record = await session.get(DocumentUpload,item.upload_id)
                eligible = False
                if record is not None:
                    try:
                        ready = await CourseService().get_ready_context(session,item.course.id)
                        eligible = item.upload_id in ready.document_ids
                    except CourseNotReadyError:
                        pass
            return {"metadata":record is not None,"eligible":eligible,
                "pending":UploadService.is_deleting(record) if record is not None else False,
                "vectors":vector.count(context["collection"],count_filter=models.Filter(must=[
                    models.FieldCondition(key="upload_id",match=models.MatchValue(value=item.upload_id))]),exact=True).count,
                "concepts":(await rows(driver,"MATCH (n:Concept {upload_id:$id}) RETURN count(n) AS n",id=item.upload_id))[0]["n"],
                "source":await asyncio.to_thread(storage.exists,item.key)}

        protected = await seed()
        protected_state = await state(protected)
        target = await seed(course=protected.course)
        assert (await request(sessions,storage,service,target.upload_id,authenticated=False)).status_code == 401
        initial_target = await state(target)
        assert initial_target == {"metadata":True,"eligible":True,"pending":False,"vectors":1,"concepts":2,"source":True}
        checks["unauthenticated_delete_preserves_all_stores"] = True
        assert (await request(sessions,storage,service,target.upload_id)).status_code == 204
        assert await state(target) == {"metadata":False,"eligible":False,"pending":False,"vectors":0,"concepts":0,"source":False}
        assert await state(protected) == protected_state
        checks["authenticated_delete_removes_target_and_preserves_neighbor"] = True

        absent = {"metadata":False,"eligible":False,"pending":False,"vectors":0,"concepts":0,"source":False}
        async def subprocess_run(manifest, *, item=None, boundary="", recover=False):
            command = [sys.executable,str(Path(__file__).resolve()),"--context",str(manifest)]
            if recover:
                command.append("--recover-child")
            else:
                command += ["--child","--upload-id",item.upload_id,"--boundary",boundary]
            process = await asyncio.create_subprocess_exec(*command,
                stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
            try:
                stdout,stderr = await asyncio.wait_for(process.communicate(),40)
                expected = 0 if recover else -signal.SIGKILL
                assert process.returncode == expected, stderr[-1200:]
                return json.loads(stdout) if recover else None
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()

        with tempfile.TemporaryDirectory(prefix="cg-delete-context-",dir="/private/tmp") as directory:
            manifest = Path(directory)/"context.json"
            manifest.write_text(json.dumps(context))
            for boundary in ("before_intent_commit","intent","vectors","graph","storage","before_metadata_commit","metadata"):
                target = await seed()
                await subprocess_run(manifest,item=target,boundary=boundary)
                interrupted = await state(target)
                observations["kill_"+boundary] = interrupted
                print(json.dumps({"boundary":boundary,"interrupted_state":interrupted}),flush=True)
                if boundary == "before_intent_commit":
                    assert interrupted == initial_target, "Uncommitted removal touched remote stores"
                    assert (await subprocess_run(manifest,recover=True))["removed"] == 0
                    assert await state(target) == initial_target
                    assert (await request(sessions,storage,service,target.upload_id)).status_code == 204
                    checks["uncommitted_deletion_preserves_ready_document_and_all_artifacts"] = True
                elif boundary == "metadata":
                    assert interrupted == absent
                    # A late remote graph write after the initial cleanup must be
                    # discovered from retained intent even though source rows are gone.
                    async with sessions() as session:
                        intent = await session.get(GraphPublicationIntent,target.intent_id)
                    try:
                        await service.replace_graph_extraction(graph(target.upload_id),target.course.id,
                            upload_id=target.upload_id,document_name="fixture.pdf",course_name=target.course.display_name,
                            execution_token=intent.document_task_id,marker=GraphProjectionMarker(intent.id,intent.job_id,
                            intent.upload_id,intent.course_id,intent.document_task_id,intent.payload_sha256,intent.source_generation))
                    except ValueError:
                        assert args.projection_fencing
                    else:
                        assert not args.projection_fencing
                        assert (await state(target))["concepts"] == 2
                    await subprocess_run(manifest,recover=True)
                    assert await state(target) == absent
                    try:
                        await service.replace_graph_extraction(graph(target.upload_id),target.course.id,
                            upload_id=target.upload_id,document_name="fixture.pdf",course_name=target.course.display_name,
                            execution_token=intent.document_task_id,marker=GraphProjectionMarker(intent.id,intent.job_id,
                            intent.upload_id,intent.course_id,intent.document_task_id,intent.payload_sha256,intent.source_generation))
                    except ValueError:
                        pass
                    else:
                        raise AssertionError("Late marked write bypassed committed deletion tombstone")
                    check_name = ("committed_deletion_blocks_late_graph_replay_before_reconciliation"
                        if args.projection_fencing else "committed_deletion_discovers_late_graph_write_and_blocks_replay")
                    checks[check_name] = True
                else:
                    assert interrupted["metadata"] and interrupted["pending"] and not interrupted["eligible"], (
                        "Interrupted deletion still exposes a READY upload with incomplete evidence")
                    assert (await request(sessions,storage,service,target.upload_id,operation="retry")).status_code == 409
                    assert (await request(sessions,storage,service,target.upload_id,operation="preview")).status_code == 409
                    async with sessions() as session:
                        assert await UploadService().retry_upload(session,target.upload_id,str(uuid4())) is None
                        record = await session.get(DocumentUpload,target.upload_id)
                        assert record.stage == "READY" and record.attempt_count == 1
                        checkpoints = (await session.execute(select(GraphExtractionCheckpoint).where(
                            GraphExtractionCheckpoint.upload_id == target.upload_id))).scalars().all()
                        assert len(checkpoints) == 1
                    async with sessions() as session:
                        summaries = await CourseService().list_summaries(session)
                        summary = next(item for item in summaries if item.course.id == target.course.id)
                        assert summary.ready_documents == 0
                    if boundary == "vectors":
                        recovered = await asyncio.gather(subprocess_run(manifest,recover=True),
                                                         subprocess_run(manifest,recover=True))
                        assert sum(result["removed"] for result in recovered) == 1
                        checks["concurrent_recovery_processes_finish_one_removal"] = True
                    else:
                        assert (await subprocess_run(manifest,recover=True))["removed"] == 1
                    assert await state(target) == absent
                    checks["hard_kill_"+boundary+"_resumes_in_new_process_without_ready_leak"] = True
                assert (await subprocess_run(manifest,recover=True))["removed"] == 0
                assert await state(protected) == protected_state

            for boundary in ("vectors","graph","storage","before_metadata_commit"):
                target = await seed()
                failing = InjectedIngestion(driver,vector,context,boundary,"raise")
                if boundary == "storage":
                    def unavailable(key):
                        raise ObjectStorageError("isolated storage outage")
                    with patch.object(storage,"delete",unavailable):
                        response = await request(sessions,storage,failing,target.upload_id)
                else:
                    response = await request(sessions,storage,failing,target.upload_id)
                assert response.status_code == (500 if boundary == "before_metadata_commit" else 503)
                failed = await state(target)
                assert failed["pending"] and not failed["eligible"]
                assert (await subprocess_run(manifest,recover=True))["removed"] == 1
                assert await state(target) == absent
                checks["failure_"+boundary+"_preserves_durable_retry"] = True

            first = await seed()
            second = await seed(course=first.course,key=first.key)
            failing = InjectedIngestion(driver,vector,context,"vectors","raise")
            assert (await request(sessions,storage,failing,first.upload_id)).status_code == 503
            assert (await state(first))["source"] is True
            assert (await state(second))["eligible"] is True
            async with sessions() as session:
                summaries = await CourseService().list_summaries(session)
                summary = next(item for item in summaries if item.course.id == first.course.id)
                assert summary.ready_documents == 1
            assert (await subprocess_run(manifest,recover=True))["removed"] == 1
            assert (await state(first))["source"] is True
            assert (await state(second))["eligible"] is True
            assert (await request(sessions,storage,service,second.upload_id)).status_code == 204
            assert await state(first) == absent and await state(second) == absent
            checks["shared_source_survives_until_last_reference_is_deleted"] = True
            first = await seed()
            second = await seed(course=first.course,key=first.key)
            responses = await request(sessions,storage,service,[first.upload_id,second.upload_id])
            assert [response.status_code for response in responses] == [204,204]
            assert await state(first) == absent and await state(second) == absent
            checks["concurrent_shared_source_deletion_leaves_no_orphan"] = True
            assert await state(protected) == protected_state
            await subprocess_run(manifest,recover=True)
            async with sessions() as session:
                # Deliberately retained intents/receipts are recovery evidence,
                # while removed documents' checkpoint/attempt rows must cascade.
                remaining = (await session.execute(select(GraphExtractionCheckpoint.upload_id))).scalars().all()
                assert remaining == [protected.upload_id]
                assert await UploadService().list_pending_deletion_ids(session) == []
                cleaned = (await session.execute(select(GraphPublicationCleanupReceipt))).scalars().all()
                assert len(cleaned) >= 1
            markers = await rows(driver,"MATCH (m:GraphPublication) RETURN m.upload_id AS upload_id")
            assert markers == [{"upload_id":protected.upload_id}]
            checks["removed_checkpoints_and_markers_leave_only_protected_upload"] = True
            checks["unrelated_ready_upload_survives_all_crash_and_failure_scenarios"] = True
    finally:
        cleanup_errors = []
        async def cleanup_async(operation):
            try:
                await operation
            except Exception as exc:
                cleanup_errors.append(type(exc).__name__)
        if driver is not None:
            await cleanup_async(driver.close())
        if vector is not None:
            try:
                vector.close()
            except Exception as exc:
                cleanup_errors.append(type(exc).__name__)
        if engine is not None:
            await cleanup_async(engine.dispose())
        if schema_created:
            try:
                async with admin.begin() as connection:
                    await connection.execute(text(f"DROP SCHEMA {schema} CASCADE"))
            except Exception as exc:
                cleanup_errors.append(type(exc).__name__)
        await cleanup_async(admin.dispose())
        for container in reversed(containers):
            try:
                docker("rm","--force","--volumes",container)
            except Exception as exc:
                cleanup_errors.append(type(exc).__name__)
        if network is not None:
            try:
                docker("network","rm",network)
            except Exception as exc:
                cleanup_errors.append(type(exc).__name__)
        if cleanup_errors:
            raise RuntimeError("Disposable resource cleanup failed: " + ", ".join(cleanup_errors))
    report = {
        "generated_at_utc":datetime.now(timezone.utc).isoformat(),
        "passed":all(checks.values()) and all(processor_checks.values())
            and all(vector_cleanup_checks.values()) and all(worker_result["checks"].values())
            and worker_result["activation_safe"] and all(lifecycle_result["checks"].values()),
        "checks":checks, "crash_observations":observations, "elapsed_seconds":round(time.perf_counter()-started,3),
        "projection_fencing_enabled_in_fixture": args.projection_fencing,
        "processor_checks": processor_checks,
        "vector_cleanup_checks": vector_cleanup_checks,
        "full_lifecycle": lifecycle_result if args.full_lifecycle else None,
        "worker_crash_qualification": worker_result if args.worker_crashes else None,
        "hosted_provider_calls":0, "production_test_data_mutations":0,
        "temporary_schema_containers_network_removed":True, "container_images":images,
        "postgres_version":postgres_version, "process_failure_mode":(
            "SIGKILL at seven deletion boundaries and during three accepted worker PUTs (two coordinator-only, one full worker)"
            if args.full_lifecycle else "SIGKILL at seven deletion boundaries and during two accepted worker PUTs"
            if args.worker_crashes else "SIGKILL at instrumented commit boundaries"),
        "source_sha256":{path:hashlib.sha256((ROOT/path).read_bytes()).hexdigest() for path in (
            "app/api/endpoints/ingest.py","app/services/upload_service.py","app/services/course_service.py",
            "app/services/demo_retention_service.py","app/services/graph_continuation_service.py",
            "app/services/ingestion_service.py","app/services/storage_service.py","scripts/evaluate_upload_deletion.py",
            "app/core/config.py","app/services/document_processing_service.py",
            "app/services/parser_service.py","app/core/blocking.py",
            "scripts/processor_fencing_checks.py","tests/test_processor_graph_fencing.py",
            "scripts/processor_crash_checks.py","scripts/deferred_vector_proxy.py",
            "tests/test_deferred_vector_proxy.py","app/core/processing_coordinator.py",
            "app/models/document_upload.py","app/services/resource_budget_service.py",
            "app/services/vector_cleanup_service.py","tests/test_vector_deletion_cleanup.py",
            "scripts/vector_cleanup_checks.py",
            "scripts/worker_lifecycle_checks.py","tests/test_worker_lifecycle_fixture.py",
            "app/worker.py","app/main.py","app/services/worker_presence_service.py",
            "app/core/security.py","app/services/security_service.py","app/core/database.py",
            "app/api/endpoints/query.py",
            "app/services/rag_service.py","app/services/citation_service.py",
            "app/services/retrieval_candidates.py","tests/test_vector_attempt_scope.py")},
        "limits":[
            "The deletion matrix uses in-process ASGI requests; full-lifecycle mode additionally uses real loopback HTTP. Neither is a Render test.",
            "PostgreSQL, Qdrant and Neo4j are real disposable stores; synthetic 3-dimensional vectors are inserted without embeddings.",
            "MinIO exercises real local S3 PUT/HEAD/DELETE requests, not hosted Cloudflare R2.",
            "Seven explicit process boundaries use SIGKILL; four additional boundaries raise exceptions. Other packet-loss/outage timings are untested.",
            "Deletion-matrix recovery uses fresh processes calling production sweeps. Full-lifecycle mode additionally exercises the real local worker and retention timer; neither qualifies deployed-worker operation.",
            "Automatic graph-only continuation remains disabled; this is not graph-job completion or an ingestion load benchmark.",
            "Existing in-flight queries that already loaded READY context are not cancelled by a subsequent deletion request.",
            "The opt-in processor checks use a native synthetic PDF, authored graph outputs and a fixed 3-dimensional encoder; no AI/OCR semantic quality is measured.",
            ("Two coordinator-only schedules kill subprocesses after their complete PUT reaches the loopback proxy. Owned lease expiry is advanced and fresh coordinators recover to READY. These schedules do not exercise full entry points; full-lifecycle mode records its additional separate API/worker experiment independently. Arbitrary network faults and load remain untested."
             if args.worker_crashes else "Ingestion cancellation is graceful and drained, not a worker hard kill; delayed Qdrant writes after process death and legacy pre-rollout writers remain unqualified."),
            "A failed late-deletion qualification blocks activation; explicitly manual fixture cleanup is not application recovery. Mixed legacy writers remain unqualified.",
            "Projection fencing is enabled only in the private fixture configuration; no production environment or automatic continuation is activated.",
            ("Full-lifecycle mode runs real local HTTP and worker entry points with an actual 60-second retention timer; embedding/graph/reranking/synthesis are authored adapters, not semantic or provider-capacity measurements. Fixture lease/presence/due timestamps are advanced, but lifecycle and timer methods are not mocked."
             if args.full_lifecycle else "Full API/worker entry points and automatic retention timer are not exercised by this mode."),
        ]}
    output = ROOT/("evaluation/worker-lifecycle-current" if args.full_lifecycle else
        "evaluation/vector-deletion-recovery-current" if args.worker_crashes else
        "evaluation/processor-fencing-current" if args.projection_fencing else "evaluation/upload-deletion-current")
    output.with_suffix(".json").write_text(json.dumps(report,indent=2)+"\n")
    output.with_suffix(".md").write_text(
        ("# Separate API, full worker lifecycle and automatic cleanup\n\n" if args.full_lifecycle else
         "# Durable vector cleanup and killed-worker recovery\n\n" if args.worker_crashes else
         "# Opt-in processor and deletion fencing — isolated stores\n\n" if args.projection_fencing
         else "# Isolated document deletion recovery\n\n") +
        f"Generated: {report['generated_at_utc']}.\n\n"
        "Command: `.venv/bin/python scripts/evaluate_upload_deletion.py --socket-dir <private-test-socket>"
        + (" --projection-fencing" if args.projection_fencing else "")
        + (" --worker-crashes" if args.worker_crashes else "")
        + (" --full-lifecycle" if args.full_lifecycle else "") + "`.\n\n"
        +f"Overall qualification: **{'PASS' if report['passed'] else 'FAIL — keep disabled'}**.\n\n"
        +f"{len(checks)}/{len(checks)} deletion regression checks passed. Zero hosted provider calls or production mutations. "
        "Temporary resources were removed.\n\n"
        +"\n".join(f"- {name}: pass" for name in checks)
        +f"\n\nProcessor checks: {len(processor_checks)} passed.\n\n"
        +"\n".join(f"- {name}: pass" for name in processor_checks)
        +("\n\n## Durable cleanup checks\n\n"+"\n".join(f"- {name}: pass" for name in vector_cleanup_checks)
          if vector_cleanup_checks else "")
        +("\n\n## Worker crash observations\n\n"+"\n".join(f"- {name}: pass" for name in worker_result["checks"])
          +"\n\n```json\n"+json.dumps(worker_result["observations"], indent=2)+"\n```\n" if args.worker_crashes else "")
        +("\n\n## Full entry points and timer\n\n"+"\n".join(f"- {name}: pass" for name in lifecycle_result["checks"])
          +"\n\n```json\n"+json.dumps(lifecycle_result["observations"], indent=2)+"\n```\n" if args.full_lifecycle else "")
        +"\n\n## Limits\n\n"+"\n".join(f"- {limit}" for limit in report["limits"])
        +"\n\nThe adjacent JSON records exact crash observations, image IDs and source hashes.\n")
    print(json.dumps({"checks_passed":len(checks),"processor_checks":len(processor_checks),
        "worker_checks": len(worker_result["checks"]), "qualification_passed": report["passed"],
        "vector_cleanup_checks": len(vector_cleanup_checks),
        "lifecycle_checks": len(lifecycle_result["checks"]),
        "hosted_provider_calls":0,"temporary_resources_removed":True}))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket-dir",type=Path)
    parser.add_argument("--port",type=int,default=55432)
    parser.add_argument("--child",action="store_true")
    parser.add_argument("--recover-child",action="store_true")
    parser.add_argument("--context",type=Path)
    parser.add_argument("--upload-id")
    parser.add_argument("--boundary")
    parser.add_argument("--projection-fencing", action="store_true")
    parser.add_argument("--worker-crashes", action="store_true")
    parser.add_argument("--full-lifecycle", action="store_true")
    args = parser.parse_args()
    if args.worker_crashes and not args.projection_fencing:
        parser.error("--worker-crashes requires --projection-fencing; production defaults must stay unchanged")
    if args.full_lifecycle and not args.worker_crashes:
        parser.error("--full-lifecycle requires the scoped crash/deletion qualification")
    asyncio.run(child(args) if args.child or args.recover_child else run(args))
