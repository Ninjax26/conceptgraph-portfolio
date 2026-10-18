# Graph resume snapshot budgets and retention

Internal Phase 3 safeguards. Automatic graph continuation remains disabled.

## Admission

- `GRAPH_SNAPSHOT_MAX_JOBS`: maximum retained nonempty snapshot bodies; zero disables this cap.
- `GRAPH_SNAPSHOT_MAX_BYTES`: maximum sum of PostgreSQL JSONB text bytes; zero disables this cap.
- Both settings default to zero. No environment or production configuration was changed.
- The existing 8 MiB per-snapshot canonical JSON bound still applies independently.

Scheduling locks its document and job, then takes a transaction-scoped installation admission lock. It flushes earlier staging in the transaction without committing, counts other uploads' nonempty snapshots, and admits only if both enabled limits allow it. Concurrent schedulers cannot both reserve the final slot. Old terminal bodies count until pruned. Duplicate scheduling for the same execution remains idempotent; replacement excludes its old body. Denial returns no new job, preserving the document and existing checkpoints.

Use the existing one-document processing transaction per admission. Do not add unrelated document acquisition after taking the global admission lock. This serialized admission scan is intended for a bounded demo; profile before proposing it as large-corpus capacity.

These are **logical payload budgets**, not PostgreSQL physical disk usage: indexes, job metadata, TOAST/compression, WAL, backups, graph checkpoints and source PDFs are excluded. Disabled or inconsistent limits across writers do not provide a hard installation quota.

## Terminal snapshot cleanup

`GraphContinuationService.prune_terminal_snapshots(session, older_than_days=..., limit=...)` is an internal, unscheduled operation. Caller owns the commit/rollback boundary; no production cleanup was run.

It selects at most 100 old `stopped`/`completed` jobs, taking document locks with `SKIP LOCKED`, then rechecks each job under its lock. It replaces only the snapshot body with `{}`. It does not delete the job, reset attempts, change digests, touch READY results, delete graph checkpoints, or call Qdrant, Neo4j or R2. Keeping the terminal row prevents a duplicate scheduling request from resurrecting exhausted automatic attempts. Empty terminal bodies no longer consume snapshot count/byte admission capacity, but their small tombstone rows remain stored.

Pending/running/recent jobs are ineligible. There is no automatic eviction of active jobs to make room. A rollback restores the snapshot body. Once deliberately committed, body pruning is not a general undo operation; later manual document processing can parse the retained source again under the original retry workflow.

## Verification limits

The isolated PostgreSQL runner tests exact logical-byte boundaries, concurrent count admission, cleanup rollback, retry-tombstone preservation, checkpoint preservation and unchanged READY results. No production snapshots were pruned. These tests do not complete publication, cross-store crash reconciliation, periodic maintenance dispatch or physical-storage capacity testing.
