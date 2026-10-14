# Durable graph-job foundation — not activated

Phase 3 increment, 13 October 2026. This implements **PostgreSQL job metadata, source snapshots, claiming and section-checkpoint fencing**. [Opt-in atomic READY/snapshot staging](GRAPH_SNAPSHOT_STAGING.md) is now connected to completion, but defaults off. It is not connected to an API route, the coordinator or a graph-only processor. Existing production uploads retain the original pipeline/manual retry behavior. No automatic production graph work is enabled.

## What is implemented

- One durable job per upload, linked by a cascading foreign key. The existing document state machine is unchanged.
- A serialized chunk snapshot bound to the upload, canonical course, source content hash/storage key, filename and existing evidence task. Native extraction/OCR/chunking need not be repeated by the future graph worker to reconstruct this input.
- SHA-256 checks for snapshot integrity and the current extraction contract. Model/prompt/schema/batch/global-linking changes invalidate compatibility; they do not silently reset attempts.
- A logical **8 MiB canonical UTF-8 JSON bound**, chunk-count limits and source-page/identity checks. This is not a physical PostgreSQL disk-size or memory-sandbox guarantee. Installation-wide snapshot storage accounting remains pending.
- Database-clock due times that cannot shorten the READY result's persisted provider reset. A demo batch budget alone is not treated as a provider-quota reset.
- Duplicate scheduling is idempotent: it cannot move the due time, replenish attempts or restart a stopped job for the same evidence task.
- Independent claim tokens and leases. Fresh locked document/job reads reject stale task IDs, owners, tokens and expired leases.
- Claims consume a bounded attempt even if the claimant subsequently crashes. The initial allowance is eight minus the document's existing attempt count; database constraints enforce the bound and complete running-lease state.
- Claiming uses `FOR UPDATE OF document_uploads SKIP LOCKED`, then locks the job. Every job operation follows **document → job → checkpoint** order; busy deletion/retry fences are skipped, not bypassed.
- A provider reset defers the job durably and clears its old claim. An expired running lease can be claimed with a new token; exhausted jobs stop instead of looping.
- Changed document tasks, expired retention, corrupted snapshots and changed extraction contracts stop the job without altering READY evidence or calling providers.
- Validated section outputs can be saved to the existing per-upload checkpoint table under the independent graph-job fence. Unknown source/structure output is rejected. The original ingestion worker's active-task/lease guards are not weakened.

| Identity | Purpose | Changed by a graph-only claim? |
| --- | --- | --- |
| Document task ID | Existing READY vector/graph evidence scope | No |
| Graph job ID | Durable continuation for that evidence task | No |
| Claim token + owner + expiry | Authority for one job execution | Yes; old claims become invalid |

These SQL methods are internal services, not user authentication. Shared reviewer access and protected endpoints remain unchanged; private workspaces are deferred.

## Schema and rollback

`GraphContinuationJob` adds `graph_continuation_jobs` through the existing schema transaction/advisory lock and `Base.metadata.create_all`. It adds no columns to existing tables and rewrites no existing upload rows. Due/lease indexes and status/attempt/lease constraints are created with the new table.

Local checks create the schema, restart initialization twice, preserve jobs across initialization and exercise old document-query SQL. A rollback uses the older application release and leaves the new table in place; older code does not query it. Do **not** drop production tables or queued metadata as a rollback shortcut. No destructive production migration is required. These checks establish additive SQL compatibility, not a full old-release deployment rehearsal.

## Verification

- [Sixteen isolated PostgreSQL checks](../evaluation/graph-continuation-jobs-current.md), PostgreSQL 18.4, UTF-8, private local Unix socket with no TCP listener.
- Three actual Python claim processes: exactly one claims a single due job while READY identity and query eligibility remain unchanged.
- One claimant is actually killed with SIGKILL. The test then explicitly expires its lease in the fixture and verifies recovery/fencing/checkpoint retention. This is a **metadata claimant test**, not a real ingestion-worker hard-kill test or a natural lease-duration timing measurement.
- Deletion holds the document fence; a second process skips that row. SQL deletion cascades the job and checkpoints, and the deleted claim cannot write again.
- Nine offline snapshot/due-time unit checks; all **418 backend tests**, nine frontend tests, TypeScript/Vite build, compile and diff checks pass.
- Hosted LLM/embedding/reranking calls: **0**. Production test-data mutations: **0**. Fixture vector/graph/source counts and graph output are authored; no cross-store consistency or semantic-quality claim is made.

Reproduce against a generated, owned private test cluster only:

```bash
.venv/bin/python scripts/evaluate_graph_continuation_jobs.py --socket-dir <private-test-socket>
.venv/bin/python -m unittest tests.test_graph_continuation_snapshot -v
.venv/bin/python -m unittest discover -s tests -q
npm run test:frontend
npm run build
.venv/bin/python -m compileall -q app scripts tests
git diff --check
```

The runner rejects ordinary database URLs and accepts only an owned, private `/private/tmp/conceptgraph-budget-pg.*` socket directory. It creates a randomized isolated schema and removes that schema in `finally`; reports include runtime/test hashes and disclose fixtures/limits. No environment files or credentials are modified.

## Next implementation gate

The following are **not implemented** and must be completed before activating automatic graph work:

1. Atomic job/snapshot staging with a quota-limited READY transition is implemented and locally verified behind a default-off staging flag. Do not enable it yet, backfill existing production PDFs or schedule a demo batch-budget result automatically.
2. Connect bounded due-job dispatch to the existing coordinator/separate worker and implement a graph-only processor using this snapshot. Do not rotate the document evidence task, reparse/OCR, re-embed, clean up vectors, or make READY documents ineligible while waiting/processing.
3. [Recovery-pass checkpoint support](GRAPH_RECOVERY_CHECKPOINTS.md) is now implemented and verified. Add a completion/publish path: projection must be source/task/job fenced, atomic in Neo4j, and cancellation-safe against concurrent deletion/retry; never release the document fence before external writes have actually settled.
4. Integrate snapshot storage accounting and retention with installation budgets. Terminal snapshots should not become unbounded retained copies.
5. Verify real separate API/ingestion processes, full worker hard-kill recovery and interrupted deletion/projection consistency against isolated stores. The current metadata checks do not satisfy those acceptance criteria.
6. Keep production scheduling disabled until the operating quota policy and all above tests are verified. Continue the existing free combined deployment; no paid worker is authorized.

Phase 3 remains partial. Hosted embedding/reranking admission, actual provider token accounting and cross-upload validated extraction caching are also still pending.
