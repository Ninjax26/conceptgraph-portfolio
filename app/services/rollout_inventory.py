"""Unscheduled read-only observations of supplied processes, never fleet/drain attestation."""
from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib
import json
import os
import re
from urllib.parse import urlsplit
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.engine import make_url

from app.core.blocking import run_blocking_safely
from app.services.vector_rollout_audit import VectorReadContract

BACKENDS = frozenset({"postgres", "neo4j", "qdrant", "source"})
# Fresh interpreters have distinct nonces even when containers both use PID 1.
# Forked children inherit the nonce but have a different PID in their namespace.
_PROCESS_NONCE = uuid4().hex
POLICY_FIELDS = ("shared_provider_scope", "shared_provider_limits_enabled", "graph_continuation_snapshots_enabled",
    "provider_requests_per_minute", "provider_requests_per_day", "provider_estimated_tokens_per_minute",
    "provider_estimated_tokens_per_day", "provider_interactive_request_reserve_per_minute",
    "provider_interactive_request_reserve_per_day", "provider_interactive_token_reserve_per_minute",
    "provider_interactive_token_reserve_per_day")


def _sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _valid_sha(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _endpoint(url):
    parsed = urlsplit(url)
    defaults = {"http":80,"https":443,"bolt":7687,"bolt+s":7687,"bolt+ssc":7687,
                "neo4j":7687,"neo4j+s":7687,"neo4j+ssc":7687}
    port = parsed.port if parsed.port is not None else defaults.get(parsed.scheme)
    if parsed.scheme not in defaults or not parsed.hostname or not port or parsed.query or parsed.fragment:
        raise ValueError("Unsupported inventory endpoint.")
    return [parsed.scheme, parsed.hostname.casefold(), port, parsed.path.rstrip("/")]


async def collect_inventory(config, engine, graph, vector, storage, *, code_sha256):
    """Use only dedicated read transactions, schema reads and HEAD; clients remain caller-owned."""
    if (config.processing_role not in {"api","worker","combined"} or not _valid_sha(code_sha256)
        or any(type(getattr(config, field)) is not bool for field in
               ("graph_projection_fencing_enabled", "graph_continuation_snapshots_enabled", "shared_provider_limits_enabled"))):
        raise ValueError("Invalid inventory export identity.")
    manifest = {"version":1,"role":config.processing_role,"pid":os.getpid(),
        "instance_sha256":_sha([_PROCESS_NONCE,os.getpid()]),
        "captured_at":datetime.now(timezone.utc).isoformat(),"code_sha256":code_sha256,
        "policy_sha256":_sha({field:getattr(config,field) for field in POLICY_FIELDS}),
        "scoped":config.graph_projection_fencing_enabled,"probes":{}}

    async def probe(name, operation):
        try:
            signature = await operation()
            manifest["probes"][name] = {"scope_sha256":_sha(signature),"error_type":None}
        except Exception as error:
            manifest["probes"][name] = {"scope_sha256":None,"error_type":type(error).__name__[:64]}

    async def postgres():
        url = make_url(config.postgres_dsn)
        if url.get_backend_name() != "postgresql" or (url.port is not None and not 1 <= url.port <= 65535):
            raise ValueError("Invalid PostgreSQL namespace.")
        # Use the actual dialect's query-override precedence instead of a second
        # URL parser contract. Ambiguous/multihost targets remain unsupported.
        _, options = url.get_dialect()().create_connect_args(url)
        host, database_name = options.get("host"), options.get("database")
        port = options.get("port")
        port = 5432 if port is None else port
        if (not isinstance(host,str) or not host or not isinstance(database_name,str) or not database_name
            or type(port) is not int or not 1 <= port <= 65535):
            raise ValueError("Invalid PostgreSQL namespace.")
        async with engine.connect() as conn:
            await conn.execute(text("SET TRANSACTION READ ONLY"))
            row = (await conn.execute(text(
                "SELECT current_database() AS database_name, current_schema() AS schema_name"))).mappings().one()
        if not all(isinstance(row[k],str) and row[k] for k in ("database_name","schema_name")):
            raise ValueError("Unknown PostgreSQL namespace.")
        return [host if host.startswith("/") else host.casefold(),port,database_name,
                row["database_name"],row["schema_name"]]

    async def neo4j():
        target = _endpoint(config.neo4j_uri)
        async with graph.session(default_access_mode="READ") as session:
            row = await (await session.run("CALL db.info() YIELD id, name RETURN id, name")).single()
        if row is None or not all(isinstance(row[k],str) and row[k] for k in ("id","name")):
            raise ValueError("Unknown graph namespace.")
        return [target,row["id"],row["name"]]

    async def qdrant():
        _endpoint(config.qdrant_url)
        contract = VectorReadContract.from_settings(config)
        info = await run_blocking_safely(vector.get_collection, contract.collection_name)
        vectors = info.config.params.vectors
        if type(getattr(vectors,"size",None)) is not int or vectors.size != contract.dimension:
            raise ValueError("Incompatible vector schema.")
        distance = str(getattr(vectors,"distance","")).casefold()
        if distance != "cosine":
            raise ValueError("Incompatible vector distance.")
        return [contract.store_sha256,contract.embedding_sha256,contract.dimension,distance]

    async def source():
        if config.object_storage_backend != "s3" or not isinstance(config.s3_bucket,str) or not config.s3_bucket:
            raise ValueError("This probe requires an S3-compatible source namespace.")
        client = storage.client
        target = _endpoint(client.meta.endpoint_url)
        declared = _endpoint(config.s3_endpoint_url) if config.s3_endpoint_url else None
        response = await run_blocking_safely(client.head_bucket, Bucket=config.s3_bucket)
        if response.get("ResponseMetadata",{}).get("HTTPStatusCode") != 200:
            raise ValueError("Source namespace HEAD was not successful.")
        return [declared,target,config.s3_bucket,config.s3_region,config.s3_force_path_style]

    for name, operation in (("postgres",postgres),("neo4j",neo4j),("qdrant",qdrant),("source",source)):
        await probe(name,operation)
    return manifest


def compare_inventories(manifests, *, now=None, max_age_seconds=300):
    """Compare only supplied fresh snapshots; unknown processes and late RPCs remain unknown."""
    result = {"supplied_instances_compatible":False,"fleet_completeness_verified":False,
        "writer_drain_verified":False,"activation_allowed":False,"differences":[]}
    now = datetime.now(timezone.utc) if now is None else now
    keys = {"version","role","pid","instance_sha256","captured_at","code_sha256","policy_sha256","scoped","probes"}
    if (not isinstance(manifests,(list,tuple)) or not 2 <= len(manifests) <= 32
        or type(max_age_seconds) is not int or not 1 <= max_age_seconds <= 300
        or not isinstance(now,datetime) or now.tzinfo is None):
        result["differences"] = ["invalid_inventory"]
        return result
    try:
        for m in manifests:
            if (not isinstance(m,Mapping) or set(m) != keys or type(m["version"]) is not int or m["version"] != 1
                or m["role"] not in {"api","worker"} or type(m["pid"]) is not int or m["pid"] < 1
                or not _valid_sha(m["instance_sha256"])
                or type(m["scoped"]) is not bool or not _valid_sha(m["code_sha256"])
                or not _valid_sha(m["policy_sha256"]) or not isinstance(m["probes"],Mapping)
                or set(m["probes"]) != BACKENDS):
                raise ValueError()
            captured = datetime.fromisoformat(m["captured_at"])
            if captured.tzinfo is None or not -5 <= (now-captured).total_seconds() <= max_age_seconds:
                raise ValueError()
            for p in m["probes"].values():
                if (not isinstance(p,Mapping) or set(p) != {"scope_sha256","error_type"}
                    or not _valid_sha(p["scope_sha256"]) or p["error_type"] is not None):
                    raise ValueError()
        if (set(m["role"] for m in manifests) != {"api","worker"}
            or len({m["instance_sha256"] for m in manifests}) != len(manifests)):
            raise ValueError()
    except (KeyError,TypeError,ValueError):
        result["differences"] = ["invalid_or_stale_inventory"]
        return result
    first = manifests[0]
    for field in ("code_sha256","policy_sha256","scoped"):
        if any(m[field] != first[field] for m in manifests[1:]):
            result["differences"].append(field)
    for backend in sorted(BACKENDS):
        if any(m["probes"][backend] != first["probes"][backend] for m in manifests[1:]):
            result["differences"].append(backend)
    result["supplied_instances_compatible"] = not result["differences"]
    return result
