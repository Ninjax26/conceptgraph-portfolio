# Atomic READY/snapshot staging — defaults off

Phase 3 increment, 13 October 2026. This connects **snapshot staging only** to completion of the existing ingestion pipeline. It does not start graph-only workers, dispatch jobs, publish graphs or enable automatic production retries.

`GRAPH_CONTINUATION_SNAPSHOTS_ENABLED` defaults to **false**. No environment files or Render settings were changed. Keep it off until the graph-only processing/publication path, installation-wide snapshot accounting and full recovery checks are verified. This is not an activation recommendation.

## What changed

- When explicitly enabled, the processor passes its exact indexed chunks to `mark_completed` only for a quota-limited graph with a known retry reset. Ordinary/default completion retains its previous call shape.
- `mark_completed` retains its existing document/task/lease and GRAPH_BUILT guards. Within that same row lock and transaction it drafts READY, updates the attempt, and stages the compatible job/snapshot before its existing commit.
- `GraphContinuationService.stage_for_ready` neither commits nor rolls back nor rereads the document. Rereading with `populate_existing` and `autoflush=False` would overwrite the caller's uncommitted READY state. The public internal `schedule` method still owns its standalone transaction and delegates to this shared staging logic.
- Snapshot identity, bounded size, model/prompt contract, due-time and attempt guards are unchanged. Already adequate/non-retryable graphs and demo-batch limits do not create continuation jobs.
- READY evidence retains its original task ID. No new parsing, OCR, embeddings, vector cleanup, source-storage requests or graph generation are added by staging.
- If explicitly requested staging fails before commit, the READY transition, attempt update and new job roll back together. The current fenced execution can retry completion. This is not a test of an ambiguous connection failure after commit; that boundary still needs full recovery validation.

No database migration, endpoint/schema change, authentication change or private workspace was introduced. No production jobs were backfilled or staged for testing.

## Verification

[Nine isolated PostgreSQL checks](../evaluation/graph-snapshot-staging-current.md) verify outside-connection visibility, rollback after flushing, retry after rollback, invalid/foreign-source rejection, ingestion lease fencing, READY query eligibility, reset preservation, default-off behavior and restart persistence. Index/source/graph counts are authored fixtures; these are transaction contracts, not full ingestion or cross-store results.

Four new offline processor-wiring/configuration tests pass. All **422 backend tests**, nine frontend tests, TypeScript/Vite build, compile and diff checks pass. The existing sixteen job-metadata and ten manual-retry PostgreSQL checks were rerun successfully on current code; their current report hashes were refreshed from those actual runs. Hosted provider calls and production test-data mutations: **0**.

```bash
.venv/bin/python scripts/evaluate_graph_snapshot_staging.py --socket-dir <private-test-socket>
.venv/bin/python -m unittest tests.test_graph_snapshot_staging -v
.venv/bin/python -m unittest discover -s tests -q
npm run test:frontend
npm run build
.venv/bin/python -m compileall -q app scripts tests
git diff --check
```

The database runner uses the existing private-socket/randomized-schema safeguards and removes its test schema in `finally`. Reports disclose mocks, limitations and runtime/test SHA-256 hashes. This increment does **not** finish Phase 3.

## Next unfinished checkpoint

Implement the graph-only processor, recovery/global checkpoints, atomic cancellation-safe Neo4j publication/completion and bounded dispatch. Preserve original READY evidence scope and fence every write against supersession/deletion. Integrate logical snapshot storage/retention accounting before activation, then validate real API/ingestion processes and full hard-kill/cross-store recovery locally. Hosted-provider admission/actual tokens and cross-upload cache remain open.
