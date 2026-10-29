# Read-only vector rollout evidence

`app/services/vector_rollout_audit.py` is an unscheduled diagnostic helper. It does not run on uploads, queries, deletion or startup, and never repairs or activates anything.

Before changing vector-read behavior, a total vector count is insufficient: that count may contain old attempts, untagged points or repeated logical chunks. This helper reads one upload's bounded metadata pages and separates:

- Current execution coverage: the expected number of distinct, well-formed chunk identities for the READY upload and course.
- Cleanup tracking: whether task tags belong to the current source execution or its known PostgreSQL attempt history.
- Configuration agreement: caller-supplied vector endpoint/collection fingerprints, embedding provider/model fingerprint, dimension and scope mode.

It accepts legacy physical point IDs when their current execution payload is valid. Known stale attempts can be excluded by scoped reads and tracked for deletion. Unknown/untagged tasks require investigation; they are never relabelled as current. Duplicate current chunks, foreign courses, invalid pages and missing current chunks fail coverage. A cursor that stops at the configured budget, cycles, returns malformed pages or encounters an RPC error cannot produce a completed result.

## Inputs and bounds

Build `VectorReadContract.from_settings` independently from each proposed API/worker vector configuration. Compare proposed scoped configurations in memory; do not enable a server flag merely to run a diagnostic. Both contracts must be validated and scoped. Credentials and raw endpoint URLs are excluded from contract values. This is configuration comparison, not inspection of live processes or proof that injected clients connect to those endpoints.

Build `ReadyVectorIdentity.from_record` from a non-deleting READY record, with a known-task set containing its current `task_id` plus historical tasks selected for that same upload. The set must include the current task even for a legacy record without an attempt-history row. Do not combine task histories from different sources or fabricate missing tags.

Call `await audit_ready_vectors(client, contract, identity, peer_contract=peer, page_size=128, max_points=4096)`. Bounds are 1–256 points per page and 1–100,000 points per upload. Only upload/course/task/chunk/page metadata is requested; neither text nor vectors is fetched. Results contain counts and error types, without source names, source text, credentials or URLs. In-flight synchronous reads drain before cancellation propagates. The caller owns the client lifecycle.

`read_coverage_ok` checks cardinality and basic metadata shape. It does not compare actual text or a separately persisted expected chunk-ID set, verify citations against PDF contents, identify the model that originally produced stored vectors, or measure semantic quality. `cleanup_payloads_trackable` does not mean cleanup has executed. A known stale execution also prevents safely rolling back to broad upload-only reads without reconciliation.

**Every result keeps `activation_allowed=false` and `writer_drain_verified=false`.** Concurrent writers can change data behind a completed cursor. Equal vector contracts do not establish PostgreSQL/Neo4j/R2 fleet compatibility, physical endpoint identity or a drained old worker/request. A fresh operational inventory, coordinated writer-drain policy and the other rollout gates remain necessary.

## Isolated qualification

The owned PostgreSQL/Qdrant/Neo4j/MinIO harness supports:

```sh
.venv/bin/python scripts/evaluate_upload_deletion.py --socket-dir <owned-private-test-socket> --projection-fencing --vector-rollout-audit
```

The harness rejects ordinary/production database socket paths and creates labelled disposable stores. Fixture preparation and teardown write synthetic data; the auditor itself is read-only and is checked against before/after metadata, vectors and a neighbor. Add `--worker-crashes --full-lifecycle` to repeat the actual API/worker/timer qualification as well. Reports use `evaluation/vector-rollout-audit-current.{json,md}` and preserve earlier reports as historical evidence.

## Startup race discovered during qualification

Concurrent recovery exposed a pre-existing PostgreSQL deadlock (`40P01`). Initializers were serialized by the schema advisory lock, but ordinary readers did not hold that lock. Repeated `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` still acquired exclusive table locks, allowing a document/publication-table lock cycle with a reader.

Initialization now inspects existing columns under the same schema transaction/advisory lock and skips only unnecessary additive column DDL. Missing columns retain the exact previous definitions; index statements and data migrations remain. A controlled probe holds an ordinary publication reader open and reproduces the old deadlock, then confirms current-schema restart completes after the fix. This does not make genuine missing-column migrations, external DDL or all index/writer lock combinations safe to run without coordination. Rollback is a code rollback; no schema/data removal is required.
