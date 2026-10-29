"""Live private peer exports and independent, owned namespace-drift probes."""
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from qdrant_client import models
from sqlalchemy import text

from app.services.rollout_inventory import collect_inventory, compare_inventories
from app.services.storage_service import StorageService
from scripts.evaluate_provider_admission import engine_for
from scripts.worker_lifecycle_checks import inventory_fixture_config, lifecycle_config


async def verify_inventory(context, sessions, driver, vector, storage, inventories):
    checks = {}
    agreement = compare_inventories(inventories)
    assert agreement["supplied_instances_compatible"], agreement
    assert all(not agreement[k] for k in ("activation_allowed","writer_drain_verified","fleet_completeness_verified"))
    checks["fresh_actual_api_worker_exports_agree_without_claiming_fleet_or_drain"] = True
    cfg = inventory_fixture_config(lifecycle_config(storage.config,"worker"), context)
    code_sha = inventories[0]["code_sha256"]
    changed_results = {}

    async def reject(label, config=cfg, *, graph=driver, vectors=vector, source=storage, engine):
        manifest = await collect_inventory(config, engine, graph, vectors, source, code_sha256=code_sha)
        # Retain the controller's actual PID. This probes intended configuration,
        # not a third running worker; only the two IPC exports establish peers.
        result = compare_inventories([inventories[0],manifest])
        assert not result["supplied_instances_compatible"], label
        changed_results[label] = result["differences"]
        checks["independent_"+label+"_namespace_drift_is_not_compatible"] = True

    schema = "cg_admission_"+uuid4().hex
    collection = "deletion_"+uuid4().hex
    bucket = "inventory-"+uuid4().hex
    own_engine = engine_for(SimpleNamespace(socket_dir=Path(context["socket_dir"]),port=context["port"]),context["schema"])
    other_engine = None
    made_schema = made_collection = made_bucket = False
    try:
        async with own_engine.begin() as conn:
            await conn.execute(text(f"CREATE SCHEMA {schema}"))
            made_schema = True
        other_engine = engine_for(SimpleNamespace(socket_dir=Path(context["socket_dir"]),port=context["port"]),schema)
        await reject("postgres_schema",engine=other_engine)
        vector.create_collection(collection,vectors_config=models.VectorParams(size=3,distance=models.Distance.COSINE))
        made_collection = True
        await reject("qdrant_collection",cfg.model_copy(update={"qdrant_collection_name":collection}),engine=own_engine)
        storage.client.create_bucket(Bucket=bucket)
        made_bucket = True
        source_cfg = cfg.model_copy(update={"s3_bucket":bucket})
        await reject("source_bucket",source_cfg,source=StorageService(source_cfg,client=storage.client),engine=own_engine)
        class OtherGraphDatabase:
            def session(self, **kwargs):
                return driver.session(database="system",**kwargs)
        await reject("graph_default_database",graph=OtherGraphDatabase(),engine=own_engine)
    finally:
        if other_engine:
            await other_engine.dispose()
        if made_schema:
            async with own_engine.begin() as conn:
                await conn.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        await own_engine.dispose()
        if made_collection:
            vector.delete_collection(collection)
        if made_bucket:
            storage.client.delete_bucket(Bucket=bucket)
    return {"checks":checks,"observations":{"supplied_peer_comparison":agreement,
        "independent_namespace_drift":changed_results,"actual_peer_count":len(inventories),
        "drift_probes_are_controller_observations_not_extra_running_workers":True,
        "physical_identity_permissions_and_unknown_instances_verified":False,
        "source_head_does_not_create_buckets":True,"hosted_model_calls":0}}
