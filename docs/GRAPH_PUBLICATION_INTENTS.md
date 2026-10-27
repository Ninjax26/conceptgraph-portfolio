# Graph publication intents — internal, not activated

`GraphPublicationIntent` is a durable record that a particular validated graph
candidate **might** be written to Neo4j. It is separate from the unpublished
`GraphPublicationDraft`. The new `stage_publication_intent` method is not called
by the API, coordinator or worker. It makes no remote write and does not complete
a graph job.

## Current contract

- The method loads and revalidates the persisted draft under the current READY
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

## Publication protocol still required

Before any caller may write remotely, it must commit the intent, reacquire the
document → job fence, verify the exact intent/draft/source identity and hold that
authority through the Neo4j write and PostgreSQL completion. A committed intent
alone is never permission to publish after the fence is released.

The disconnected Neo4j replacement primitive can now write a `GraphPublication`
marker in the same managed transaction and read it by intent ID. This marker
is remote commit evidence only when the caller supplied an exact staged intent;
no runtime caller does that yet. A separate `GraphPublicationReceipt` row can
now be staged after the current fenced job reads an exact matching marker. The
receipt survives document/job deletion, is immutable by intent ID, and is not
created when the marker is absent or conflicting. The observer now compares
the marker's generation with the stored intent and current upload generation,
then stores it in the receipt. Its caller owns commit or rollback. The
publisher still needs PostgreSQL job finalization and a
reconciler for ambiguous commits, killed processes, superseded tasks and document
deletion. Normal graph cleanup now deletes markers for the same upload and
course along with its concepts; a late write after that cleanup must still be
found and removed. The entire fenced publication/completion unit must drain on
cancellation. These steps and bounded dispatch remain unimplemented; automatic
continuation stays disabled.

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

A disconnected reconciliation helper now opens its own PostgreSQL session,
checks an intent's upload under `FOR UPDATE`, and calls the Neo4j tombstone
only if the upload is committed absent. It waits for an in-flight deletion and
does nothing if that deletion rolls back. Remote failures propagate so the
same intent can be retried. The isolated PostgreSQL runner tests these cases
with a mocked remote call; no worker or scheduler invokes this helper.

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
not call the new tombstone primitive. After PostgreSQL proves committed upload
deletion, that primitive can permanently mark the fence deleted and remove the
upload's concepts and publication markers in one managed Neo4j transaction.
Real Neo4j checks cover idempotence, course scope, prepublication deletion and
a concurrent in-flight marked write; later marked writes are rejected. It is
disconnected because the live deletion order can fail after graph cleanup, so
writing a permanent tombstone there could block a still-READY upload. The
PostgreSQL intent/receipt now persist and compare it, but a coordinator must
still prove source authority, publish, finalize and reconcile killed
or ambiguous writes across stores. Exact-marker cleanup alone cannot establish
that no writer remains in flight.

The receipt's isolated PostgreSQL test uses a mocked marker reader and the
deleted-source helper uses a mocked Neo4j tombstone. The separate isolated
Neo4j test exercises real marker and tombstone writes. They are **not** a
combined PostgreSQL–Neo4j transaction, hard-kill or full deletion-reconciliation
test. A receipt means an exact marker was observed, not that the source remains
eligible or that the graph job is complete.

## Verification

Four offline unit tests cover authority/draft gating, exact identity,
caller-owned transactions, idempotence/conflict and the absence of cascading
foreign keys. The isolated PostgreSQL graph-job runner adds two checks: a staged
intent survives schema reinitialization and is idempotent across sessions; a
separate fixture intent survives committed document/job deletion. The runner
uses a private temporary PostgreSQL cluster, removes its random schema and
reports zero hosted-provider calls. It does **not** test Neo4j publication,
receipts, cross-store crash recovery or production deletion cleanup. See the
implementation ledger and `evaluation/graph-continuation-jobs-current.md` for
the actual run and source hashes.
