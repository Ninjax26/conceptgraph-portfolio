# Graph publication intents — internal, not activated

Normal source removal now has a durable pre-cleanup flag and restart sweep;
see [deletion recovery](DELETION_RECOVERY.md). Pending removal fences graph
claims while retaining source metadata/checkpoints until remote cleanup has
finished. Its authenticated ASGI route has been tested across disposable
PostgreSQL, Qdrant, Neo4j and MinIO. The internal marked publication unit has
real-store crash checks below; hosted R2 and activation alongside ordinary
ingestion/retry remain unverified. Automatic continuation stays disabled.

`GraphPublicationIntent` is a durable record that a particular validated graph
candidate **might** be written to Neo4j. It is separate from the unpublished
`GraphPublicationDraft`. The live API, processing coordinator and worker do not
call the marked publication path. The internal `publish_publication` method now
uses `stage_publication_intent`; staging alone makes no remote write and does
not complete a graph job.

## Intent staging contract

- `stage_publication_intent` loads and revalidates the persisted draft under the current READY
  document, graph-job claim and source snapshot. It rechecks the claim before
  staging an intent. The caller owns commit or rollback; successful calls retain
  document and job locks.
- One immutable intent is allowed per job. Repeating the same call is idempotent;
  a different document identity or candidate digest is rejected. The row stores
  an ID, job ID, upload ID, original READY task ID, course ID, candidate digest
  and the positive upload attempt count as its source generation, but no source
  text or graph payload.
- The table has no document or job foreign key. A remote commit may outlive
  deletion of those rows, so its intent must remain available for future
  reconciliation. This is deliberate retention, not proof that a write happened.
- Startup creates the additive table and indexes without changing existing
  document/job columns or rows. The later generation columns are nullable for
  old rows; they remain available for deletion reconciliation but fail closed
  if replayed as a publication. Startup can add the columns repeatedly without
  changing legacy rows. Older application code can ignore them; rollback does
  not require dropping them.

## Internal marked publication protocol — dispatch disabled

Before any caller may write remotely, it must commit the intent, reacquire the
document → job fence, verify the exact intent/draft/source identity and hold that
authority through the Neo4j write and PostgreSQL completion. A committed intent
alone is never permission to publish after the fence is released.

The disconnected Neo4j replacement primitive can now write a `GraphPublication`
marker in the same managed transaction and read it by intent ID. This marker
is remote commit evidence only when the caller supplied an exact staged intent;
the internal publisher supplies it, but no live runtime caller does that yet. A separate `GraphPublicationReceipt` row can
now be staged after the current fenced job reads an exact matching marker. The
receipt survives document/job deletion, is immutable by intent ID, and is not
created when the marker is absent or conflicting. The observer now compares
the marker's generation with the stored intent and current upload generation,
then stores it in the receipt. Its caller owns commit or rollback. The
internal `complete_publication` method can now stage the observed receipt,
graph counters/result and terminal job state in one PostgreSQL transaction.
It reloads the validated draft, refuses provider-limited work, rechecks the
live source/job claim after remote observation and retains the document locks.
Caller commit makes all three durable; rollback discards them together. Original
READY task/attempt, vector count, PDF metadata, processing timings, completion
time and section checkpoints are preserved. A completed claim cannot complete
again. The initial ingestion path and this method share the existing graph
result-field formatter, including the same graph-quality classification.

This is not a publisher or reconciler for ambiguous commits, killed processes,
superseded tasks and document deletion. Normal graph cleanup now deletes
markers for the same upload and course along with its concepts; a late write after that cleanup must still be
found and removed. The entire fenced publication/completion unit must drain on
cancellation. The internal coordinator now drains that unit; ordinary ingestion/retry
interactions, delayed old writes and bounded dispatch still require verification.
Automatic continuation stays disabled.

An internal `cleanup_graph_publication` primitive can remove one exact remote
marker and only concepts carrying that marker's original READY task token in
one Neo4j transaction. It is idempotent and preserves a newer task's graph.
It has no runtime caller. A reconciler must establish in PostgreSQL that the
intent is obsolete before using it; the marker alone is not cleanup authority.

`list_deleted_source_intents` now finds at most 100 retained intents whose
upload row has been deleted, in stable creation/ID order. It only reads
PostgreSQL and has no worker or scheduler caller. An absent upload identifies
a cleanup candidate, not proof that an in-flight Neo4j write has ended. The
future reconciler must repeat after late writers have drained; this discovery
must never directly authorize a remote delete.

A reconciliation helper opens its own PostgreSQL session,
checks an intent's upload under `FOR UPDATE`, and calls the Neo4j tombstone
only if the upload is committed absent. It waits for an in-flight deletion and
does nothing if that deletion rolls back. It serializes reconcilers for the
intent with a transaction advisory lock, then persists a retained cleanup
receipt only after the remote tombstone succeeds. Remote or PostgreSQL commit
failures can be retried because the Neo4j tombstone is idempotent. Discovery
skips cleaned intents, so a bounded scan can reach later deleted uploads.
The protected combined-service retention loop now invokes a bounded scan of
50 unreceipted deleted-source intents per interval and reconciles each in a
fresh transaction. A failed item is logged and can retry on a later sweep;
the live API deletion order is unchanged. The isolated PostgreSQL runner
tests rollback, restart, repeat, pagination and concurrency with a mocked
remote call. A separate disposable PostgreSQL–Neo4j check hard-kills the
reconciler after its real tombstone commit and verifies that the sweep writes
one receipt on restart. Neither test runs the full live API, R2 or Qdrant
deletion path. The cleanup receipt has no cascading foreign key and contains
no PDF text or graph payload.

## Remote generation fence and remaining activation blockers

The isolated Neo4j test first reproduced an old exact-marker replay replacing
a newer task's graph. The disconnected marked replacement now requires an
explicit positive source generation and serializes publication on a unique
`GraphProjectionFence.upload_id` node in the same Neo4j transaction as concept
replacement and marker creation. A lower generation, or a different task or
payload at the same generation, is rejected before graph deletion. The marker also stores
the generation; the isolated test checks replay rejection against real Neo4j.
The source is the monotonically increasing PostgreSQL upload attempt, now
recorded in staged intents and observed receipts. No worker currently passes
it into the marked Neo4j writer.

**Do not activate graph-only continuation yet.** Normal document deletion does
not write a tombstone before its PostgreSQL commit. The combined-service
retention sweep can subsequently prove committed upload deletion and call the
tombstone primitive to permanently mark the fence deleted and remove the
upload's concepts and publication markers in one managed Neo4j transaction.
Real Neo4j checks cover idempotence, course scope, prepublication deletion and
a concurrent in-flight marked write; later marked writes are rejected. It is
delayed because the live deletion order can fail after graph cleanup, so
writing a permanent tombstone there could block a still-READY upload. The
PostgreSQL intent/receipt now persist and compare it, but a coordinator must
still prove source authority, publish, finalize and reconcile killed
or ambiguous writes across stores. Exact-marker cleanup alone cannot establish
that no writer remains in flight.

The receipt's isolated PostgreSQL test uses a mocked marker reader and the
deleted-source helper uses a mocked Neo4j tombstone. A separate isolated
Neo4j test exercises real marker and tombstone writes. The new combined
PostgreSQL–Neo4j runner covers one hard-kill boundary and retry through the
retention sweep. It is **not** a full API/R2/Qdrant deletion-reconciliation or
graph-job-completion test. A publication receipt means an exact marker was
observed, not that the source remains eligible or the graph job is complete.

## Verification

Four offline unit tests cover authority/draft gating, exact identity,
caller-owned transactions, idempotence/conflict and the absence of cascading
foreign keys. The isolated PostgreSQL graph-job runner adds two checks: a staged
intent survives schema reinitialization and is idempotent across sessions; a
separate fixture intent survives committed document/job deletion. The runner
uses a private temporary PostgreSQL cluster, removes its random schema and
reports zero hosted-provider calls. It does **not** test Neo4j publication,
receipts or production deletion cleanup. See the implementation ledger,
`evaluation/graph-continuation-jobs-current.md` and
`evaluation/graph-cross-store-recovery-current.md` for the distinct runs,
limits and source hashes.

## Caller-owned completion verification — 28 October

The dedicated `scripts/evaluate_graph_completion.py` runner passed twelve checks
against a disposable PostgreSQL cluster, using authored remote marker reads.
It tests missing/conflicting markers, missing draft/intent, stale/expired claims,
a natural lease expiry during a delayed marker read, pending deletion,
provider-limited output, lock retention, transactional visibility/rollback and
restart-persisted completion. It checks that original READY identity, source and
vector metadata, initial timings and extraction checkpoints remain available.

See `evaluation/graph-completion-current.json` for source fingerprints and limits.
These checks do not publish a graph, exercise Neo4j, run an ingestion-worker
process or prove crash recovery/cancellation drain across stores. The automatic
worker remains disconnected. The next activation checks cover interactions between the internal marked
publisher and ordinary ingestion/manual retry, delayed older writes and bounded
dispatch. The later real-store report below covers one publish/complete unit.

## Real-store publication recovery — 28 October

The internal `publish_publication` method owns its sessions and commits, unlike
the caller-owned staging/completion helpers. It commits its immutable intent in a
first PostgreSQL session, then uses a fresh session to reacquire document/job
locks and revalidate the current draft, intent, source generation and lease.
Under those locks it reads the exact Neo4j marker. A matching committed marker
is reused; an absent marker triggers the atomic marked replacement. A conflict
fails closed. Receipt observation, graph-result updates and terminal job state
then commit together in PostgreSQL. The existing `run_async_safely` wrapper
drains the entire operation, including final commit and session cleanup, before
propagating caller cancellation. A new claim can recover a saved draft without
model calls. `False` means no active claim was completed, including an already
completed claim; it is not permission to republish under a stale token.

`scripts/evaluate_graph_publication_recovery.py` uses real disposable PostgreSQL
and Neo4j with synthetic sources/graphs. Its twelve checks include five actual
SIGKILL boundaries (before/after intent commit, inside the Neo4j transaction,
after Neo4j commit, and after PostgreSQL result commit), a lost acknowledgement,
competing publishers, repeated cancellation while retaining the source lock,
source removal between transactions, conflicting markers and provider-limited
drafts. Recovery checks the Neo4j fence write epoch to detect unnecessary
rewrites, as well as the marker, receipt, job and retained READY/checkpoint data.

See `evaluation/graph-publication-recovery-current.json` for hashes, image ID,
crash observations, cleanup and limits. Recovery explicitly expires the private
fixture lease before acquiring a fresh token; it does not benchmark a complete
worker or natural lease-duration wait. No models, Qdrant, R2 or live API are
used. Normal ingestion/manual retry still use their existing unmarked writes;
interactions with late older writes and dispatch must be verified before any
activation. Five instrumented kills do not prove every delayed server commit or
network timing. Automatic continuation remains disabled.

The final allowed graph attempt crashing after remote commit, followed by
stopped-job reconciliation, is not covered by this matrix. The claim policy still
stops exhausted jobs; activation needs a bounded recovery policy that can observe
an already-committed marker without granting fresh provider attempts.

## Final-attempt recovery and ordinary retry probe — 28 October

`reconcile_exhausted_publication` now handles one exact retained intent in its
own cancellation-drained transaction. It accepts only a matching, retained READY
source whose graph job exhausted its attempt allowance and either stopped for
`attempt_limit` or has an expired final running lease. It never grants a claim,
increments attempts, calls a model or writes Neo4j. Under document → job locks,
it revalidates the snapshot, extraction contract, saved draft and intent, reads
the exact remote marker and commits receipt/result/job completion together.
Missing or conflicting markers cannot complete work. Source identity, initial
completion time, vector count and checkpoints remain unchanged. An already
completed job returns false before another remote observation. This helper is
internal and has no runtime dispatcher or API caller.

The dedicated `scripts/evaluate_graph_retry_final_attempt.py` passed twelve
recovery/invariant checks against disposable PostgreSQL and Neo4j, including
two actual SIGKILL boundaries: final-attempt remote commit before PostgreSQL
completion, and the read-only completion commit. It also covers concurrent
observers, repeated cancellation while retaining the source lock, bookkeeping
rollback, marker conflicts, changed extraction contracts and ineligible jobs.
Private fixture counters are advanced to the last allowed attempt and leases
are explicitly expired; no provider calls are consumed to reach that state.
The Neo4j fence write epoch stays unchanged during receipt recovery.

**Ordinary retry ordering is unsafe in the separate adversarial probe.** After
a real manual retry and its ordinary unmarked Neo4j write, replaying an exact
older marked operation replaced that newer task's two concepts: the count for
the current task fell from two to zero. PostgreSQL correctly rejects its stale
claim, but ordinary writes do not advance the remote generation fence. The probe
explicitly replays an older remote request; it is not a timed packet-delay test
or a full API/vector-ingestion benchmark. This is a confirmed activation blocker,
not a passing graph-safety assertion. No ordinary ingestion writes are changed
by this increment. All graph writers and cleanup paths need compatible generation
fencing before automatic publication can be enabled.

See [the report](../evaluation/graph-retry-final-attempt-current.md) and its JSON
for source hashes, image identity, cleanup and limits. The earlier twelve-check
publication matrix was not rerun; its historical hashes remain its own scope.
No models, Qdrant, R2 or production data were used. Phase 3 remains partial and
automatic continuation remains disabled.
