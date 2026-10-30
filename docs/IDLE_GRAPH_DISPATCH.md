# Gated idle-worker graph dispatch

The existing coordinator can now use an idle document-worker slot for one due
graph job. This does not add workers or lease jobs into an in-memory queue.
Production scheduling remains disabled; deploying code does not activate it.

Before a graph claim, all three independent flags must be exactly true:
`GRAPH_CONTINUATION_DISPATCH_ENABLED` (new, default false),
`GRAPH_CONTINUATION_SNAPSHOTS_ENABLED` and `GRAPH_PROJECTION_FENCING_ENABLED`.
The role must be worker/combined, shared provider budgets must be enabled, and
both UTC request windows must reserve positive interactive headroom. If a token
limit is enabled, its corresponding interactive token reserve must be positive.
These are admission headroom checks, not preemption or a latency guarantee;
actual graph calls still pass through the existing shared graph_extraction budget.
No flags, limits, credentials or deployment roles are changed by this increment.

An enabled worker waits for queued documents first. Only a queue timeout and
still-empty queue permit one durable `claim_next` call. The existing database
source/job locks, skip-locked claim selection, due time, attempt limits and
lease token remain authoritative across processes. A lease starts when a worker
is available to run it. A new upload arriving after claiming can wait for that
slot; graph work is not preempted. At most PROCESSING_CONCURRENCY total ingestion
and graph executions run in this coordinator; the existing document queue remains
bounded. Continuous queued ingestion may delay graph jobs intentionally.

The claim transaction drains before cancellation returns. A cancelled but
committed claim is left to existing lease recovery, never handed to a detached
processor. The one-claim processor retains its authoritative snapshots,
checkpoints, immutable drafts and drained fenced publication. Known provider
limits persist their deadline and clear the claim, so later idle polls cannot
bypass the reset. Other failures are logged once per execution, with no inline
retry; existing lease/attempt bounds determine later eligibility. No parser,
embedding, vector/source-object write or original READY task rotation is added.

The ordinary default-off worker retains its prior blocking document-queue path.
The dispatcher is lifecycle-owned by the existing worker task: no extra graph
background loop or untracked process survives coordinator stop/restart.

Verification is scoped to offline scheduling contracts and owned local
PostgreSQL/Neo4j coordinator checks with authored extraction. The integration
fixture exercises real coordinator start/stop and lazy processor construction,
real claims, snapshots, drafts, projection markers and receipts, but the
coordinators share one event loop. It does not qualify separate OS graph workers,
four-store ingestion, semantic answer/graph correctness or hosted provider costs.
The prior five instrumented publisher SIGKILL boundaries remain distinct from
full graph-worker crash/restart tests. Fixture setup explicitly makes authored
jobs due; publisher recovery explicitly expires test leases.

Command:

```sh
.venv/bin/python scripts/evaluate_graph_publication_recovery.py \
  --socket-dir <private-test-socket> --idle-graph-dispatch
```

[Report](../evaluation/idle-graph-dispatch-current.md) and
[source hashes/observations](../evaluation/idle-graph-dispatch-current.json).

Next gates: bounded exhausted-publication discovery/reconciliation and safe
terminal snapshot/draft retention in runtime; combined/separate-worker crash
recovery and consistency qualification before any activation. Provider usage
completeness and versioned cross-upload caching also remain unfinished.
Phase 3 is partial. Keep all production scheduling/projection flags disabled.
