"""Isolated PostgreSQL admission checks, including actual subprocess contention.

Only an explicitly supplied private temporary Unix socket directory is accepted.
No provider, embedding, reranking, graph, source-storage or production call.
This is not a separate-ingestion-worker benchmark or a vendor token measurement.
"""

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import time
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core import database
from app.core.config import Settings
from app.core.exceptions import SharedProviderBudgetExceeded
from app.services import shared_provider_service as shared


def engine_for(args, schema=None):
    directory = args.socket_dir.resolve()
    if (directory.parent != Path("/private/tmp") or not directory.name.startswith("conceptgraph-budget-pg.")
            or directory.stat().st_uid != os.getuid() or stat.S_IMODE(directory.stat().st_mode) & 0o077):
        raise ValueError("Use an owned, private /private/tmp/conceptgraph-budget-pg.* test socket directory.")
    if schema is not None and not re.fullmatch(r"cg_admission_[0-9a-f]{32}", schema):
        raise ValueError("Only the randomly generated admission-test schema is allowed.")
    return create_async_engine("postgresql+asyncpg://", pool_size=3, max_overflow=0, connect_args={
        "host": str(directory), "port": args.port, "user": "cg_budget", "database": "postgres", "timeout": 5,
        "server_settings": {"search_path": schema or "public", "statement_timeout": "10000"},
    })


def test_config(scope, policy):
    return Settings(_env_file=None, RERANK_PROVIDER="local", SHARED_PROVIDER_LIMITS_ENABLED=True,
                    SHARED_PROVIDER_SCOPE=scope, **policy)


async def reserve(operation, tokens):
    try:
        await shared.reserve_provider_request("groq", operation=operation, estimated_tokens=tokens)
        return True
    except SharedProviderBudgetExceeded:
        return False


async def attempt(sessions, cfg, operation, tokens):
    with patch.object(shared, "AsyncSessionLocal", sessions), patch.object(shared, "settings", cfg):
        return await reserve(operation, tokens)


async def child(args):
    engine = engine_for(args, args.schema)
    try:
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        cfg = test_config(args.scope, json.loads(args.policy))
        # Patch once per process, not once per concurrent coroutine: overlapping
        # patch contexts could restore another task's settings while it waits.
        with patch.object(shared, "AsyncSessionLocal", sessions), patch.object(shared, "settings", cfg):
            results = await asyncio.gather(*(reserve(args.operation, args.tokens) for _ in range(args.attempts)))
        print(json.dumps({"pid": os.getpid(), "accepted": sum(results), "denied": len(results) - sum(results)}))
    finally:
        await engine.dispose()


async def contend(args, schema, scope, policy, tokens, attempts):
    async def launch():
        process = await asyncio.create_subprocess_exec(
            sys.executable, str(Path(__file__).resolve()), "--child", "--socket-dir", str(args.socket_dir),
            "--port", str(args.port), "--schema", schema, "--scope", scope, "--policy", json.dumps(policy),
            "--tokens", str(tokens), "--attempts", str(attempts),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
            if process.returncode:
                raise RuntimeError("A local admission subprocess failed; no provider call was made.")
            return json.loads(stdout)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
    return await asyncio.gather(*(launch() for _ in range(3)))


async def run(args):
    admin = engine_for(args)
    schema = "cg_admission_" + uuid4().hex
    engine = None
    created = False
    checks, measurements = {}, {}
    try:
        async with admin.begin() as connection:
            postgres_version = (await connection.execute(text("SHOW server_version"))).scalar_one()
            await connection.execute(text(f"CREATE SCHEMA {schema}"))
            created = True
        engine = engine_for(args, schema)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        # Simulate the exact six-column table from the previous deployed release.
        async with engine.begin() as connection:
            await connection.execute(text("CREATE TABLE provider_budgets (scope VARCHAR(80), provider VARCHAR(16), "
                "minute_window BIGINT NOT NULL, day_window BIGINT NOT NULL, minute_requests INTEGER NOT NULL, "
                "day_requests INTEGER NOT NULL, PRIMARY KEY(scope,provider))"))
            await connection.execute(text("INSERT INTO provider_budgets VALUES ('legacy','groq',1,2,3,4)"))
        with patch.object(database, "postgres_engine", engine):
            await asyncio.gather(database.initialize_database_schema(), database.initialize_database_schema())
        async with engine.connect() as connection:
            row = (await connection.execute(text("SELECT * FROM provider_budgets WHERE scope='legacy'"))).mappings().one()
            assert (row["minute_window"], row["day_window"], row["minute_requests"], row["day_requests"]) == (1,2,3,4)
            assert row["minute_estimated_tokens"] == row["day_estimated_tokens"] == 0
        checks["concurrent_additive_migration_preserves_legacy_counts"] = True
        async with engine.begin() as connection:
            # Previous release INSERT/UPDATE remains valid without knowing new columns.
            await connection.execute(text("INSERT INTO provider_budgets (scope,provider,minute_window,day_window,minute_requests,day_requests) "
                                          "VALUES ('old_client','gemini',1,2,3,4)"))
            await connection.execute(text("UPDATE provider_budgets SET day_requests=5 WHERE scope='old_client'"))
        checks["old_release_sql_compatible_without_dropping_usage"] = True

        policy = {"PROVIDER_REQUESTS_PER_MINUTE":100, "PROVIDER_REQUESTS_PER_DAY":8,
                  "PROVIDER_INTERACTIVE_REQUEST_RESERVE_PER_DAY":2}
        rows = await contend(args, schema, "priority", policy, tokens=400, attempts=4)
        assert len({row["pid"] for row in rows}) == 3
        assert sum(row["accepted"] for row in rows) == 6
        assert sum(row["denied"] for row in rows) == 6
        cfg = test_config("priority", policy)
        assert all([await attempt(sessions, cfg, "answer_synthesis", 400) for _ in range(2)])
        assert not await attempt(sessions, cfg, "answer_synthesis", 400)
        async with engine.connect() as connection:
            row = (await connection.execute(text("SELECT day_requests,day_estimated_tokens FROM provider_budgets WHERE scope='priority'"))).one()
            assert tuple(row) == (8,3200)
        measurements["request_priority"] = {"processes":3, "background_accepted":6, "background_denied":6,
                                             "interactive_accepted":2, "total_requests":8, "reserved_estimated_tokens":3200}
        checks["separate_process_request_holdback_and_total_cap"] = True

        policy = {"PROVIDER_REQUESTS_PER_MINUTE":100, "PROVIDER_REQUESTS_PER_DAY":100,
                  "PROVIDER_ESTIMATED_TOKENS_PER_DAY":2800}
        rows = await contend(args, schema, "tokens", policy, tokens=700, attempts=3)
        assert sum(row["accepted"] for row in rows) == 4
        assert sum(row["denied"] for row in rows) == 5
        async with engine.connect() as connection:
            row = (await connection.execute(text("SELECT day_requests,day_estimated_tokens FROM provider_budgets WHERE scope='tokens'"))).one()
            assert tuple(row) == (4,2800)
        measurements["token_admission"] = {"processes":3, "accepted":4, "denied":5,
                                            "total_requests":4, "reserved_estimated_tokens":2800}
        checks["separate_process_token_cap_no_overspend"] = True

        cfg = test_config("denial", policy)
        assert not await attempt(sessions, cfg, "graph_extraction", 2801)
        async with engine.connect() as connection:
            assert (await connection.execute(text("SELECT count(*) FROM provider_budgets WHERE scope='denial'"))).scalar_one() == 0
        assert await attempt(sessions, cfg, "graph_extraction", 700)
        checks["denial_rolls_back_insert_and_releases_lock"] = True

        cfg = test_config("failure", {})
        def fail():
            raise RuntimeError("simulated provider failure")
        with patch.object(shared, "AsyncSessionLocal", sessions), patch.object(shared, "settings", cfg):
            try:
                await shared.observe_provider_call("groq", "answer_synthesis", fail, input_characters=400)
            except RuntimeError:
                pass
            else:
                raise AssertionError("Fixture failure was not raised")
        async with engine.connect() as connection:
            row = (await connection.execute(text("SELECT day_requests,day_estimated_tokens FROM provider_budgets WHERE scope='failure'"))).one()
            assert tuple(row) == (1,1300)
        checks["failed_attempt_retains_committed_reservation"] = True

        # Cancellation while waiting for the same transaction lock cannot send
        # a vendor request or consume a reservation that never committed.
        cfg = test_config("cancel", {})
        async with engine.begin() as blocker:
            await blocker.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                                  {"key":"conceptgraph:provider:cancel:groq"})
            task = asyncio.create_task(attempt(sessions, cfg, "answer_synthesis", 100))
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    waiting = (await blocker.execute(text(
                        "SELECT EXISTS (SELECT 1 FROM pg_locks waiting JOIN pg_locks held "
                        "USING (locktype,database,classid,objid,objsubid) WHERE waiting.locktype='advisory' "
                        "AND NOT waiting.granted AND held.granted AND held.pid=pg_backend_pid() "
                        "AND waiting.pid<>held.pid)"
                    ))).scalar_one()
                    if waiting:
                        break
                    await asyncio.sleep(.02)
                assert waiting, "Admission never reached the held database lock"
            finally:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                else:
                    raise AssertionError("Blocked admission did not cancel")
        async with engine.connect() as connection:
            assert (await connection.execute(text("SELECT count(*) FROM provider_budgets WHERE scope='cancel'"))).scalar_one() == 0
        assert await attempt(sessions, cfg, "answer_synthesis", 100)
        checks["cancelled_lock_wait_no_reservation_or_leaked_lock"] = True

        async with engine.begin() as connection:
            await connection.execute(text("INSERT INTO provider_budgets VALUES ('reset','groq', "
                "floor(extract(epoch FROM clock_timestamp())/60)-1, floor(extract(epoch FROM clock_timestamp())/86400)-1, "
                "1000,1000,1000000,1000000)"))
        cfg = test_config("reset", {"PROVIDER_ESTIMATED_TOKENS_PER_MINUTE":1000, "PROVIDER_ESTIMATED_TOKENS_PER_DAY":1000})
        assert await attempt(sessions, cfg, "answer_synthesis", 500)
        async with engine.connect() as connection:
            row = (await connection.execute(text("SELECT minute_requests,day_requests,minute_estimated_tokens,day_estimated_tokens "
                                                  "FROM provider_budgets WHERE scope='reset'"))).one()
            assert tuple(row) == (1,1,500,500)
        checks["expired_windows_reset_counts_and_estimates_together"] = True
        async with engine.begin() as connection:
            await connection.execute(text("INSERT INTO provider_budgets VALUES ('minute_reset','groq', "
                "floor(extract(epoch FROM clock_timestamp())/60)-1, floor(extract(epoch FROM clock_timestamp())/86400), "
                "1000,3,1000000,600)"))
        minute_cfg = test_config("minute_reset", {"PROVIDER_ESTIMATED_TOKENS_PER_MINUTE":1000, "PROVIDER_ESTIMATED_TOKENS_PER_DAY":2000})
        assert await attempt(sessions, minute_cfg, "answer_synthesis", 200)
        async with engine.connect() as connection:
            row = (await connection.execute(text("SELECT minute_requests,day_requests,minute_estimated_tokens,day_estimated_tokens "
                                                  "FROM provider_budgets WHERE scope='minute_reset'"))).one()
            assert tuple(row) == (1,4,200,800)
        checks["minute_reset_preserves_current_daily_usage"] = True
        with patch.object(shared, "AsyncSessionLocal", sessions), patch.object(shared, "settings", cfg):
            status = await shared.shared_provider_status()
        assert status["usage"][0]["day_estimated_tokens"] == 500
        checks["status_scoped_and_matches_committed_usage"] = True
    finally:
        if engine is not None:
            await engine.dispose()
        if created:
            async with admin.begin() as connection:
                await connection.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        await admin.dispose()
    report = {"generated_at":datetime.now(timezone.utc).isoformat(), "postgres_version":postgres_version,
              "checks":checks, "measurements":measurements,
              "code_sha256":{name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in (
                  "app/core/config.py", "app/core/database.py", "app/models/document_upload.py",
                  "app/services/provider_budget_policy.py", "app/services/shared_provider_service.py",
                  "app/services/provider_failover.py",
                  "scripts/evaluate_provider_admission.py")},
              "provider_calls":0, "test_schema_removed":True,
              "limitations":["Real PostgreSQL and separate admission processes, not separate API/ingestion workers.",
                             "No hosted LLM/embedding/reranking/R2/Neo4j calls; no production load or answer-quality claim.",
                             "Token reservations are explicit fixture estimates, not vendor-reported usage.",
                             "Existing application budgets/data were not accessed; the isolated random schema was removed.",
                             "Estimated-token limits and interactive reserves default to zero (opt-in)."]}
    output = ROOT/args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.with_suffix(".json").write_text(json.dumps(report, indent=2)+"\n")
    output.with_suffix(".md").write_text("# Phase 3 provider admission check\n\n"+
        "Real isolated PostgreSQL; three separate admission processes per contention case; no provider calls.\n\n"+
        "\n".join(f"- {name}: passed" for name in checks)+"\n\n## Measurements\n\n"+
        "\n".join(f"- {name}: `{json.dumps(value, sort_keys=True)}`" for name,value in measurements.items())+
        "\n\n## Limits\n\n"+"\n".join(f"- {item}" for item in report["limitations"])+"\n")
    print(json.dumps({"checks_passed":len(checks), "provider_calls":0, "test_schema_removed":True}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket-dir", required=True, type=Path)
    parser.add_argument("--port", type=int, default=55432)
    parser.add_argument("--output", default="evaluation/provider-admission-current")
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--schema", help=argparse.SUPPRESS)
    parser.add_argument("--scope", help=argparse.SUPPRESS)
    parser.add_argument("--policy", help=argparse.SUPPRESS)
    parser.add_argument("--tokens", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--attempts", type=int, default=1, help=argparse.SUPPRESS)
    parser.add_argument("--operation", default="graph_extraction", help=argparse.SUPPRESS)
    args = parser.parse_args()
    asyncio.run(child(args) if args.child else run(args))
