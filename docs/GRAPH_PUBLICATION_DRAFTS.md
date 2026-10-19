# Saved graph candidates — local, not activated

`GraphPublicationDraft` and the internal `save_publication_draft` /
`load_publication_draft` methods retain one exact validated candidate per graph
job. No API, coordinator or worker calls them. They do not publish to Neo4j,
complete a job, re-embed evidence or alter the READY document identity.

## Transaction and storage contract

- Document/job authority, persisted snapshot and extraction contract are checked
  first, in existing lock order. Candidate validation uses those current sources.
- The job row serializes cooperating writers. Draft row reads use a row lock and
  fresh ORM values. Save/load flush pending changes but never commit or roll back
  successful transactions; the caller owns the transaction, including validation
  failures. Lost authority follows the existing rollback behavior.
- Same-transaction and later identical saves are idempotent. A different validated
  candidate cannot overwrite the original. A retry may change its claim token,
  but it must still establish current authority before reading the saved draft.
- The text and checksum are retained exactly, not reserialized through JSONB.
  Reads verify checksum, structure, source fingerprint and current identity.
- Each body is limited to 8 MiB of UTF-8 text in code and PostgreSQL. The primary
  key also indexes its job foreign key. This is a logical per-row bound, not a
  physical disk/WAL/memory limit or installation-wide budget.
- Job deletion cascades the unpublished draft. No draft is a remote-commit receipt,
  deletion tombstone or authorization to perform a late external write.

## Additive schema and rollback

The existing schema initializer creates only the new `graph_publication_drafts`
table; it adds no columns to existing tables or rewrites to existing data. Isolated
checks create/reinitialize the schema with a saved draft and preserve existing
READY results. Older application SQL does not refer to the draft table; rolling
back application code can leave it in place. Do not drop production tables as a
rollback shortcut. Actual release/deployment status is recorded separately in the
implementation ledger; schema compatibility checks are not deployment evidence.

## Verification and remaining activation gates

Local follow-up adds `GRAPH_DRAFT_MAX_JOBS` and `GRAPH_DRAFT_MAX_BYTES`, both
zero/default-disabled. These are separate installation-wide draft count/logical
UTF-8 body budgets; snapshot budgets retain their original meaning. Admission
uses the same transaction-scoped installation lock as snapshot admission and
flushes earlier same-transaction writes. All retained draft statuses count; an
identical existing draft remains usable at the cap. Denial stages nothing and
leaves the caller's transaction/READY result unchanged. All writers must use
consistent configured limits; these do not create capacity or provider quota.

`prune_terminal_drafts` is unscheduled and bounded to 1–100 rows. It locks document,
job and draft in that order, skips busy documents and removes only drafts whose
job is stopped/completed and older than the requested 1–365-day cutoff. Successful
calls leave commit/rollback to the caller. It preserves job identity, attempt
history, snapshots, checkpoints and READY output; repeated cleanup in one
transaction sees pending deletes. Snapshot cleanup refreshes job `updated_at`, so
draft retention starts from that newer timestamp rather than bypassing recency.

The follow-up passes 505 backend tests and 33 isolated PostgreSQL checks. A first
database run expected immediate pruning after snapshot cleanup and failed; the
fixture now explicitly verifies recent-job exclusion before ageing test metadata.
Byte/count limits, cap-safe idempotency, cleanup rollback and preservation pass.
Concurrent competing draft admissions and busy-row cleanup are not newly verified
by these tests. No activation, production pruning or deployment is claimed.

Subsequent verification completes those two local checks: 35 PostgreSQL checks
pass, including two concurrent sessions admitting exactly one draft into the last
slot while preserving READY output, and cleanup skipping another session's locked
document. All 505 offline backend tests pass again. These are two-session metadata
contracts, not full independent workers or cross-system crash recovery. The report
records current source hashes; no hosted AI requests or production writes occur.

Six offline boundary tests and all 501 backend tests pass. The isolated PostgreSQL
runner passes 31 checks, including same-transaction idempotency, rollback, saved
candidate survival across schema reinitialization/new sessions, stale-claim
rejection, conflicting-body rejection, an actual SQL empty-body constraint and
cascade deletion rolled back without changing the READY result. Fixtures use
mocked extraction; no Qdrant/Neo4j/R2 consistency claim is made.

Before dispatch or production activation, choose explicit snapshot/draft limits and a maintenance policy,
and implement a distinct durable remote-write intent/receipt protocol. Test ambiguous commits, deletion races and
hard-kill reconciliation across actual stores. Cascading this unpublished draft
must **not** be reused as the design for a late-write reconciliation record.
Automatic continuation remains disabled. No hosted AI calls or production test
data mutations were made.
