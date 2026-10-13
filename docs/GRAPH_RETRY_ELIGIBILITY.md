# Phase 3 — durable graph retry eligibility

This increment prevents premature **manual** graph retries. It does not introduce automatic graph continuation, a new queue, or a separate ingestion-worker deployment.

## Behavior

- A quota-limited section batch retains its normalized retry delay. The optional relationship-recovery pass now retains this information too, instead of silently losing a quota failure after successful section extraction.
- SDK `Retry-After` headers and normalized provider/application exceptions are supported. Vendor delays respect the existing circuit's minimum cooldown; application holdbacks use their own reset. Missing/invalid delays use the existing provider cooldown. Delays are bounded between one second and seven days, not treated as an infinite scheduling promise.
- The processing result stores `graph_retry_after_seconds`. Under the existing completion row lock, PostgreSQL's `clock_timestamp()` determines an absolute `graph_retry_not_before` in `result_json`. No new table, column or migration is required.
- The locked retry service rechecks this deadline using database time. A premature request returns HTTP `429` and an integer `Retry-After`; it does not increment attempts, change the task, erase READY results, call source storage or enqueue work.
- During the wait, the document remains READY/vector-search eligible. Deletion remains available for eligible READY documents and cascades their checkpoints normally.
- Once due, the existing retry state machine is used. The document becomes active/UPLOADED and consumes one bounded attempt, as before. Existing graph checkpoints are retained; graph extraction skips successfully completed batches. A successful quota-free extraction clears the old delay.
- Old records without the new metadata keep their existing retry behavior. Malformed/naive legacy timestamps are ignored rather than creating a permanent new retry prohibition. Generated timestamps are timezone-aware.

The initial parse/chunk/vector steps still run on an accepted retry. "Checkpoint-only resume" here describes the **LLM graph batches**, not a completed graph-only pipeline optimization. Provider keys, public sample, auth, resource limits and READY scope are unchanged.

## Confirmed concurrency bug fixed

The real concurrent API test initially failed with a duplicate `(document_id, attempt_number)` insert. Each request had read a READY record before acquiring the retry lock. SQLAlchemy could then return that identity-map object's stale values even though PostgreSQL had acquired the row lock after another request changed the document to UPLOADED.

Locked upload reads now use `populate_existing=True` before state decisions: retry, claim, deletion, fenced updates and stale recovery. This does not replace row locks, lease ownership or task-token fencing; it ensures those checks examine the current database state. It also prevents a cached READY deletion read from deleting a newly active attempt and a cached unclaimed read from stealing a committed worker lease.

No fields are refreshed after local mutations: service operations fetch/lock before changing state and commit their mutations before the next operation. Tests use the application's `autoflush=False` session behavior.

## Actual verification

[PostgreSQL/API report](../evaluation/graph-retry-current.md): ten checks pass against a private socket-only PostgreSQL 18.4 fixture. Recorded code hashes identify the checked implementation.

- Deadline survives a fresh process and rejects its premature retry.
- Three concurrent premature API retries return `429` without altering the original task, READY state, result, attempt count or checkpoint.
- The waiting document remains in PostgreSQL's READY retrieval scope.
- After making the authored fixture deadline expire, two concurrent API retries produce exactly one accepted retry and one `409`, with only one additional processing attempt.
- Completed checkpoints survive the accepted retry; the original task is fenced from writing a new checkpoint.
- Stale session objects cannot steal a new lease or permit deletion after the document becomes active.
- An eligible waiting document can be deleted; its checkpoint is removed by the existing PostgreSQL cascade.
- Legacy records without a deadline continue to retry normally.

Eleven focused unit regressions cover delay normalization, invalid metadata, HTTP boundaries, completed-batch reuse, optional-link quota reporting and refresh-on-locked-read contracts. Full backend suite: **389 passing tests**. Nine frontend tests, TypeScript/Vite build, compile checks and diff checks pass. No model, embedding, reranking, Neo4j, Qdrant or R2 call was made by the new checks.

These are real PostgreSQL/ASGI checks, with source-existence/enqueue doubles and authored committed chunk/graph counts. They do not establish real ingestion-worker recovery, hard-kill safety, vendor availability, production throughput or cross-store cleanup correctness. Authentication/middleware is covered by the existing suite, not this bare isolated ASGI app.

## Reproduce

Use the private temporary PostgreSQL setup in [provider admission](PROVIDER_ADMISSION.md). The runner accepts only an explicitly supplied, owned private test socket directory and removes only its randomly generated test schema.

```bash
.venv/bin/python scripts/evaluate_graph_retry.py --socket-dir "$admission_fixture"
.venv/bin/python -m unittest tests.test_graph_retry_policy -v
.venv/bin/python -m unittest discover -s tests -q
```

## Still pending in Phase 3

Automatic due-job continuation, a graph-only path avoiding repeat embedding work, genuine separate API/ingestion-worker and hard-kill tests, hosted embedding/reranking admission, actual provider token usage, and versioned cross-upload extraction cache. Do not enable production holdbacks or claim Phase 3 is complete merely because manual retry timing passed.
