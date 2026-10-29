import copy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from app.core.config import Settings
from app.services.rollout_inventory import collect_inventory, compare_inventories
from app.services import rollout_inventory as inventory_module


def settings(role="api", **changes):
    cfg = Settings(_env_file=None, PROCESSING_ROLE=role, SHARED_PROVIDER_LIMITS_ENABLED=True,
        DATABASE_URL="postgresql+asyncpg://user:privatepass@localhost:5432/data",
        NEO4J_URI="bolt://user:privatepass@localhost:7687", QDRANT_URL="http://localhost:6333",
        QDRANT_COLLECTION_NAME="vectors", EMBEDDING_DIMENSION=3, OBJECT_STORAGE_BACKEND="s3",
        S3_ENDPOINT_URL="http://localhost:9000", S3_BUCKET="privatebucket", S3_AUTO_CREATE_BUCKET=True,
        GRAPH_PROJECTION_FENCING_ENABLED=True)
    return cfg.model_copy(update=changes)


def clients():
    row = {"database_name":"data", "schema_name":"public"}
    conn = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(
        mappings=lambda: SimpleNamespace(one=lambda:row))))
    pg_context = MagicMock()
    pg_context.__aenter__ = AsyncMock(return_value=conn)
    pg_context.__aexit__ = AsyncMock(return_value=False)
    engine = SimpleNamespace(connect=Mock(return_value=pg_context))
    graph_session = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(
        single=AsyncMock(return_value={"id":"database-id", "name":"neo4j"}))))
    graph_context = MagicMock()
    graph_context.__aenter__ = AsyncMock(return_value=graph_session)
    graph_context.__aexit__ = AsyncMock(return_value=False)
    graph = SimpleNamespace(session=Mock(return_value=graph_context))
    vector = SimpleNamespace(get_collection=Mock(return_value=SimpleNamespace(config=SimpleNamespace(
        params=SimpleNamespace(vectors=SimpleNamespace(size=3,distance="Cosine"))))))
    source_client = SimpleNamespace(head_bucket=Mock(return_value={"ResponseMetadata":{"HTTPStatusCode":200}}),
        create_bucket=Mock(), meta=SimpleNamespace(endpoint_url="http://localhost:9000"))
    storage = SimpleNamespace(client=source_client, check_ready=Mock())
    return engine, graph, vector, storage, conn


class RolloutInventoryTests(unittest.IsolatedAsyncioTestCase):
    async def manifest(self, role="api", **changes):
        engine, graph, vector, storage, conn = clients()
        # Simulate independently imported processes; actual PID/nonce export is
        # checked by the separate-process integration fixture.
        with patch.object(inventory_module,"_PROCESS_NONCE",role):
            manifest = await collect_inventory(settings(role, **changes), engine, graph, vector, storage,
                code_sha256="a"*64)
        return manifest, (engine, graph, vector, storage, conn)

    async def test_collector_projects_only_digests_and_read_only_observations(self):
        manifest, (_, graph, vector, storage, conn) = await self.manifest()
        self.assertTrue(all(p["scope_sha256"] for p in manifest["probes"].values()))
        self.assertNotIn("privatepass", repr(manifest))
        self.assertNotIn("privatebucket", repr(manifest))
        self.assertNotIn("localhost", repr(manifest))
        self.assertEqual(str(conn.execute.call_args_list[0].args[0]), "SET TRANSACTION READ ONLY")
        self.assertEqual(conn.execute.await_count, 2)
        self.assertIn("db.info", str(graph.session.return_value.__aenter__.return_value.run.call_args.args[0]))
        vector.get_collection.assert_called_once_with("vectors")
        storage.client.head_bucket.assert_called_once_with(Bucket="privatebucket")
        storage.client.create_bucket.assert_not_called()
        storage.check_ready.assert_not_called()

    async def test_rotated_url_credentials_preserve_namespace_signatures(self):
        first, _ = await self.manifest()
        second, _ = await self.manifest(database_url="postgresql+asyncpg://other:newpass@LOCALHOST:5432/data",
            neo4j_uri="bolt://other:newpass@LOCALHOST:7687", qdrant_url="http://new:newpass@LOCALHOST:6333/")
        self.assertEqual(first["probes"], second["probes"])

    async def test_each_probe_error_is_partial_and_sanitized_without_stopping_other_reads(self):
        for backend in ("postgres", "neo4j", "qdrant", "source"):
            engine, graph, vector, storage, conn = clients()
            failing = {"postgres":conn.execute, "neo4j":graph.session.return_value.__aenter__.return_value.run,
                       "qdrant":vector.get_collection, "source":storage.client.head_bucket}[backend]
            failing.side_effect = RuntimeError("privatepass privatebucket localhost source contents")
            r = await collect_inventory(settings(), engine, graph, vector, storage, code_sha256="a"*64)
            self.assertEqual(r["probes"][backend]["error_type"], "RuntimeError")
            self.assertIsNone(r["probes"][backend]["scope_sha256"])
            self.assertEqual(sum(bool(p["scope_sha256"]) for p in r["probes"].values()), 3)
            self.assertNotIn("privatepass", repr(r))

    async def test_matching_known_peers_never_certify_fleet_or_drain_or_activation(self):
        api, _ = await self.manifest()
        worker, _ = await self.manifest("worker")
        worker["pid"] += 1
        r = compare_inventories([api, worker])
        self.assertTrue(r["supplied_instances_compatible"])
        for field in ("activation_allowed", "writer_drain_verified", "fleet_completeness_verified"):
            self.assertFalse(r[field])

    async def test_backend_policy_code_and_scope_differences_fail_comparison(self):
        api, _ = await self.manifest()
        worker, _ = await self.manifest("worker")
        worker["pid"] += 1
        for backend in api["probes"]:
            changed = copy.deepcopy(worker)
            changed["probes"][backend]["scope_sha256"] = "b"*64
            self.assertFalse(compare_inventories([api, changed])["supplied_instances_compatible"])
        for field, value in (("policy_sha256","b"*64), ("code_sha256","b"*64), ("scoped",False)):
            changed = copy.deepcopy(worker)
            changed[field] = value
            self.assertFalse(compare_inventories([api, changed])["supplied_instances_compatible"])

    async def test_manifest_role_freshness_completeness_and_types_fail_closed(self):
        api, _ = await self.manifest()
        worker, _ = await self.manifest("worker")
        worker["pid"] += 1
        for changes in ({"pid":True}, {"instance_sha256":api["instance_sha256"]}, {"scoped":"false"}, {"code_sha256":""},
            {"role":"api"}, {"version":True}, {"unexpected":"privatepass"},
            {"captured_at":(datetime.now(timezone.utc)-timedelta(hours=1)).isoformat()},
            {"captured_at":(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()}):
            r = compare_inventories([api, worker | changes])
            self.assertFalse(r["supplied_instances_compatible"])
            self.assertNotIn("privatepass", repr(r))
        incomplete = copy.deepcopy(worker)
        incomplete["probes"].pop("source")
        self.assertFalse(compare_inventories([api, incomplete])["supplied_instances_compatible"])
        self.assertFalse(compare_inventories([api])["supplied_instances_compatible"])

    async def test_budget_namespace_and_limits_are_part_of_shared_policy(self):
        api, _ = await self.manifest()
        for changes in ({"shared_provider_scope":"other"}, {"provider_requests_per_day":299},
                        {"shared_provider_limits_enabled":False}, {"graph_continuation_snapshots_enabled":True}):
            peer, _ = await self.manifest("worker", **changes)
            peer["pid"] += 1
            self.assertFalse(compare_inventories([api, peer])["supplied_instances_compatible"])

    async def test_invalid_collection_or_head_response_cannot_certify_backend(self):
        engine, graph, vector, storage, _ = clients()
        vector.get_collection.return_value.config.params.vectors.size = 4
        storage.client.head_bucket.return_value = {"ResponseMetadata":{"HTTPStatusCode":403}}
        r = await collect_inventory(settings(), engine, graph, vector, storage, code_sha256="a"*64)
        self.assertIsNone(r["probes"]["qdrant"]["scope_sha256"])
        self.assertIsNone(r["probes"]["source"]["scope_sha256"])

    async def test_invalid_export_identity_is_rejected_before_io(self):
        for changes in ({"processing_role":"unknown"}, {"graph_projection_fencing_enabled":"false"}):
            engine, graph, vector, storage, _ = clients()
            with self.assertRaises(ValueError):
                await collect_inventory(settings(**changes), engine, graph, vector, storage, code_sha256="a"*64)
            engine.connect.assert_not_called()

    async def test_container_local_pids_need_not_be_globally_unique(self):
        api, _ = await self.manifest()
        worker, _ = await self.manifest("worker")
        api["pid"] = worker["pid"] = 1
        self.assertTrue(compare_inventories([api,worker])["supplied_instances_compatible"])

    async def test_postgres_query_host_and_port_override_authority(self):
        first, _ = await self.manifest(database_url="postgresql+asyncpg://u@localhost:5432/data?host=storea&port=7654")
        same, _ = await self.manifest(database_url="postgresql+asyncpg://u@storea:7654/data")
        other, _ = await self.manifest(database_url="postgresql+asyncpg://u@localhost:5432/data?host=storeb&port=7654")
        self.assertEqual(first["probes"]["postgres"],same["probes"]["postgres"])
        self.assertNotEqual(first["probes"]["postgres"],other["probes"]["postgres"])

    async def test_postgres_unix_socket_path_case_is_preserved(self):
        first, _ = await self.manifest(database_url="postgresql+asyncpg://u@/data?host=%2Ftmp%2FStoreA")
        other, _ = await self.manifest(database_url="postgresql+asyncpg://u@/data?host=%2Ftmp%2Fstorea")
        self.assertNotEqual(first["probes"]["postgres"],other["probes"]["postgres"])

    async def test_explicit_invalid_postgres_ports_never_become_default(self):
        for url in ("postgresql+asyncpg://u@localhost:0/data", "postgresql+asyncpg://u@localhost/data?port=0",
                    "postgresql+asyncpg://u@localhost/data?port=65536"):
            r, _ = await self.manifest(database_url=url)
            self.assertIsNone(r["probes"]["postgres"]["scope_sha256"])
