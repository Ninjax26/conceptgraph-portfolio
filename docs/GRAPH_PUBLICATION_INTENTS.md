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
  an ID, job ID, upload ID, original READY task ID, course ID and candidate digest,
  but no source text or graph payload.
- The table has no document or job foreign key. A remote commit may outlive
  deletion of those rows, so its intent must remain available for future
  reconciliation. This is deliberate retention, not proof that a write happened.
- Startup creates the additive table and indexes without changing existing
  document/job columns or rows. Older application code can ignore the table;
  rollback does not require dropping it.

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
created when the marker is absent or conflicting. Its caller owns commit or
rollback. The publisher still needs PostgreSQL job finalization and a
reconciler for ambiguous commits, killed processes, superseded tasks and document
deletion. Normal graph cleanup now deletes markers for the same upload and
course along with its concepts; a late write after that cleanup must still be
found and removed. The entire fenced publication/completion unit must drain on
cancellation. These steps and
bounded dispatch remain unimplemented; automatic continuation stays disabled.

The receipt's isolated PostgreSQL test uses a mocked marker reader. The separate
isolated Neo4j test exercises the real marker write/read. They are **not** a
combined PostgreSQL–Neo4j transaction, hard-kill or deletion-reconciliation
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
