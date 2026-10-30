# One-claim graph-only processor

`GraphOnlyProcessor` is an internal one-claim orchestration layer. The coordinator
may call it through separately gated [idle dispatch](IDLE_GRAPH_DISPATCH.md). Both existing projection/snapshot flags
must be exactly true and the role must be worker/combined before any I/O. Existing
production defaults, free combined deployment, endpoints and schemas are unchanged.

For one already-claimed graph job it:

1. Renews the current claim and revalidates a persisted immutable draft under the
   existing document/job/source-contract checks. A corrupt draft fails closed.
2. Reuses that draft without any provider extraction. If no draft exists, delegates
   to `GraphJobExtraction`, which reads authoritative snapshots/checkpoints, renews
   its lease and durably defers known provider-quota deadlines.
3. Acquires installation admission before document/job/draft locks, stages the
   validated output and commits. Repeated cancellation drains this whole SQL unit;
   a cancelled stage does not start publication afterward. Missing authority or
   capacity never silently permits a publish. An unknown quota deadline never
   becomes an immutable, permanently unpublishable draft.
4. Closes its sessions, then delegates to the existing durable publisher. That
   publisher commits an intent, rechecks source/job/draft authority, publishes or
   recovers the exact remote marker, and drains receipt/result completion.

The processor performs no parsing/OCR, source-object operations, embedding or
vector cleanup/writes. It preserves the original READY evidence task and source
eligibility. It processes exactly one existing claim and does not claim, poll,
reschedule errors immediately, create workers or activate any production path.
Other failures leave existing bounded lease/attempt recovery in place. Exhausted
publication recovery and terminal retention now have independently gated
[idle maintenance](GRAPH_JOB_MAINTENANCE.md); production defaults remain off.

Verification: 13 new offline contracts; 657 total backend tests; nine frontend
tests; TypeScript/Vite build. An owned PostgreSQL/Neo4j run passes 15 checks: the
existing 12 publication checks plus draft replay with zero authored extraction
calls, fresh snapshot extraction/staging/publication, and durable quota deferral
without a draft/projection change. It retains the five existing **publisher**
SIGKILL boundaries; these are not new full-processor or worker hard-kill schedules.
The real store run uses authored graphs and provides no parser/vector/source-store
interfaces. It is not hosted inference, four-store ingestion, semantic accuracy,
a load test or operational deployment of automatic graph continuation.

Command:

```sh
.venv/bin/python scripts/evaluate_graph_publication_recovery.py \
  --socket-dir <private-test-socket> --graph-only-processor
```

[Results](../evaluation/graph-only-processor-current.md) and
[observations/source hashes](../evaluation/graph-only-processor-current.json).
Fifteen source hashes match and compile. The 60.152-second fixture time is not
throughput or a latency percentile. All owned resources were removed. Docker was
initially stopped; that failed attempt produced no success report. Verification
was rerun after starting the existing local engine. No hosted calls or production
test-data mutations occurred. One fresh read-only review found no important gaps.

Next: combined/separate OS graph-worker crash and consistency qualification.
Idle dispatch and recovery/retention reuse existing worker slots behind separate
default-false gates. Keep all production flags disabled. Phase 3 remains
partial; provider-cost completeness and cross-upload cache are also unfinished.
