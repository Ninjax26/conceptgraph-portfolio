# Fenced relationship-recovery checkpoints

Phase 3 increment, 14 October 2026 (India). This extends the internal graph-job checkpoint boundary; it does **not** enable a graph-only processor, dispatch or automatic production continuation.

## Implemented

- The extraction pipeline and persisted-checkpoint validator share one deterministic recovery-request builder. Existing prompt text, source selection, model routing, limits and `global:v5` key construction are unchanged.
- A recovery identity is reconstructed from the job's validated section checkpoints in source-batch order, using the existing merge logic and recovery eligibility rules. Global, unversioned, malformed or foreign-source rows cannot authorize themselves.
- The identity includes the actual preceding concept names and selected source excerpts. If section concepts change, a recovery save using the old key is rejected.
- `GraphContinuationService.save_checkpoint` supports both section and recovery batches while holding the same document → job → checkpoint fence. The existing section-only entry point remains section-only.
- Raw schema, relationship endpoints/types, batch limits, source provenance, snapshot integrity, model contract, task ID, owner, claim token and lease expiry remain enforced. Successful explicit empty recovery results can be saved and reused instead of causing repeat requests.

The recovery helper does not infer new relationships or validate semantic truth. It only verifies that a saved response belongs to the request that the current source/section outputs would produce. Human-reviewed relationship correctness remains pending.

## Evidence and limits

- Seven new offline identity tests: actual extraction callback identity, changed concepts, invalid/foreign sources, ignored global rows, stable source order, disabled recovery and no unnecessary single-section recovery.
- [Nineteen isolated PostgreSQL job checks](../evaluation/graph-continuation-jobs-current.md), including valid/forged/stale recovery saves, changed-concept rejection, and reuse of persisted sections plus an explicit empty recovery response with **zero model calls**.
- All **429 backend tests**, nine frontend tests, TypeScript/Vite build and compile/diff checks pass. The existing twenty checkpoint-compatibility tests, nine atomic staging checks and ten manual-retry database checks were rerun on current code and their actual report hashes refreshed.
- No migration, environment changes, hosted provider calls or production test-data mutations. Indexed/graph/source counts and graph outputs in these checks are authored fixtures. They do not demonstrate semantic accuracy, actual API/ingestion-worker operation, Neo4j publication or cross-store hard-kill recovery.

```bash
.venv/bin/python -m unittest tests.test_recovery_checkpoint_identity -v
.venv/bin/python scripts/evaluate_graph_continuation_jobs.py --socket-dir <private-test-socket>
.venv/bin/python scripts/evaluate_graph_checkpoint_compatibility.py
.venv/bin/python -m unittest discover -s tests -q
npm run test:frontend
npm run build
```

Keep `GRAPH_CONTINUATION_SNAPSHOTS_ENABLED=false`. Next: graph-only processing, cancellation-safe atomic publication/completion, bounded dispatch and snapshot storage/retention accounting, then real split-worker and cross-store recovery verification. Phase 3 remains partial; phases 4–8 are not marked complete.
