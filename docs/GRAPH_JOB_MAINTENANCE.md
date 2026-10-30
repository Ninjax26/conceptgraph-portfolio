# Idle graph-job recovery and retention

`GraphJobMaintenance` is wired into the existing idle-worker slot behind new
`GRAPH_CONTINUATION_MAINTENANCE_ENABLED=false`. Dispatch, snapshot and projection
flags must also be true; worker/combined role and the coordinator's existing
shared provider/headroom gates still apply. Deployment activates none of these.
No new task loop, schema, endpoint, provider request or paid worker is introduced.

The default interval is 60 seconds per coordinator, batch size 10, body retention
seven days. Settings bound these to 60–3600 seconds, 1–100 entries and 1–365 days.
A process-local lock prevents overlapping sweeps; failures still wait until the
next interval. This is not fleet-wide rate limiting or a scheduling guarantee.

A bounded read-only UUID cursor discovers exhausted current READY jobs with a
durable intent and no completion receipt. It includes expired final leases and
stopped attempt-limit jobs. Discovery releases its session before remote reads.
The existing reconciler revalidates source/job/draft authority under locks and
accepts only the exact Neo4j marker. It never extracts, grants another attempt
or rewrites a graph. Missing or conflicting markers remain unresolved. One bad
candidate cannot hide later candidates; the cursor wraps on an empty page.

After reconciliation, a separate drained transaction takes installation admission
before source/job/draft locks, prunes up to the batch limit of old terminal drafts
and snapshots, and commits. Jobs/attempt tombstones, checkpoints, READY evidence,
intents and durable receipts remain. **An intent without a completion receipt
protects its snapshot and draft indefinitely**, even beyond retention. Filtering
and a locked recheck prevent cleanup from discarding ambiguous-commit recovery
inputs. This fixes a reproduced bug in the formerly unscheduled pruning methods.
Capacity controls remain authoritative; unresolved data is not evicted to fit.

Queued documents win before a new graph claim, including arrivals during a
maintenance sweep. Reconciliation and cleanup drain their full transactions on
cancellation before the worker releases lifecycle ownership. Graceful shutdown
is verified separately from process kills and surviving server requests.

Verification uses authored fixtures and owned local PostgreSQL/Neo4j. It covers
old unresolved-body preservation, bounded cursor discovery, exact final-marker
recovery, missing-marker refusal, receipted-body cleanup and real coordinator
wiring. Fixture terminal ages are explicitly advanced, not measured retention
waiting. Prior publisher SIGKILL checks remain publisher checks, not a full
separate-process graph-worker hard-kill or four-store capacity claim.

```sh
.venv/bin/python scripts/evaluate_graph_publication_recovery.py \
  --socket-dir <private-test-socket> --graph-maintenance
```

That mode also runs prior idle dispatch and one-claim/publisher checks.
[Report](../evaluation/graph-job-maintenance-current.md) and
[observations/hashes](../evaluation/graph-job-maintenance-current.json).
Existing PostgreSQL snapshot/draft rollback, admission and skip-locked checks are
rerun independently with `scripts/evaluate_graph_continuation_jobs.py`.

Next: qualify the actual combined and separate graph-worker lifecycle with
hard-kill/restart and cross-store consistency before rollout. Keep production
flags disabled. Provider usage completeness and versioned cross-upload caching
are also unfinished; Phase 3 remains partial.
