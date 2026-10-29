# Read-only process inventory

`app/services/rollout_inventory.py` is an unscheduled diagnostic helper. Running services do not call it automatically. It collects observations from caller-supplied clients, then compares only the supplied snapshots.

The collector uses a dedicated PostgreSQL `READ ONLY` transaction to observe database/schema, a Neo4j database-info read, a Qdrant collection-schema read and S3 `HEAD` bucket. It does not call storage readiness helpers, which may create a bucket. Unsupported local storage, missing resources, denied reads or incompatible vector schemas produce partial snapshots rather than assumed agreement. Existing clients remain caller-owned, and synchronous reads drain before cancellation.

Exports contain role, PID, timestamp, process-instance/code/policy digests and per-backend scope digests or error types. Database names, source names/text, bucket names, endpoint URLs, credentials and exception messages are not exported. The code digest is supplied by the caller; it is not a signed binary attestation. The private lifecycle fixture computes its digest from explicitly named code files.

## Comparison boundaries

`compare_inventories` validates exact manifest shapes, integer/boolean types and digests. It accepts 2–32 supplied API/worker snapshots, requires both roles, and rejects failed/partial probes, stale snapshots, timestamps more than five seconds ahead, duplicate process instances, code/policy differences and backend-scope differences. The maximum snapshot age is 300 seconds and can be tightened by the caller.

PIDs are namespace-local: different containers can both use PID 1. Instance digests therefore combine a per-interpreter nonce and PID, rather than requiring globally unique PIDs. PostgreSQL host/port query overrides follow the installed SQLAlchemy dialect's connection-argument precedence. Absolute Unix-socket paths retain case; effective ports must be valid. Ambiguous/multihost targets fail closed. URL credentials are excluded from namespace identities.

The shared policy digest covers provider-budget scope, limits, holdbacks and the continuation switch. Vector observations include configured endpoint/collection and embedding contract plus observed dimension/distance. PostgreSQL includes configured target and observed database/schema. Graph observations include configured target and observed default database identity. Source observations include configured/client endpoint and the bucket/addressing settings whose HEAD succeeded.

Agreement means these supplied observations agree. It does **not** discover unknown API/helper/worker processes, establish current liveness, prove physical endpoint identity or equal permissions, identify the historical embedding model, inspect arbitrary runtime overrides, measure capacity, or prove old requests have drained. Aliases can conservatively compare differently. A standalone collector process cannot attest to an independently running process. Exports from a combined-role instance are supported; comparison currently qualifies the separate API/worker case.

Every comparison preserves:

```json
{"fleet_completeness_verified":false,"writer_drain_verified":false,"activation_allowed":false}
```

This is a prerequisite for an operational inventory and coordinated drain/rollback rehearsal, not permission to activate either rollout flag. No production inventory or credential access was performed for qualification.

## Local evidence

The existing private four-store harness adds optional `--fleet-inventory`, requiring the actual API/full-worker lifecycle mode:

```sh
.venv/bin/python scripts/evaluate_upload_deletion.py --socket-dir <owned-private-test-socket> --projection-fencing --worker-crashes --full-lifecycle --vector-rollout-audit --fleet-inventory
```

Run fixtures with the project on `PYTHONPATH` from `/private/tmp`, as in the recorded verification, so project environment files are not loaded. The harness only accepts owned private socket directories and labelled disposable stores.

The two actual child processes export their own snapshots using the same clients/configuration used by their application lifecycle. Parent verification checks their real PIDs and fresh comparison. Independent controller probes then change an owned PostgreSQL schema, Neo4j default database, Qdrant collection and MinIO bucket. Each compares incompatible. These controller probes are not extra running workers. Setup/teardown writes only owned test resources; the collector itself remains read-only. Reports use `evaluation/fleet-inventory-current.{json,md}` and preserve earlier qualification reports.
