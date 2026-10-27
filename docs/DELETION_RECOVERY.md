# Durable document removal

Document removal now records the user's request before touching any remote
store. It uses the existing upload's result_json fields
deletion_requested=true and deletion_requested_at; no new ingestion stage,
table, migration or provider call is introduced.

## Order and authority

1. Acquire the existing PostgreSQL PDF-admission advisory lock, then the upload
   row lock. Only READY or FAILED records can enter removal.
2. Commit the deletion flag, preserving the document's task, attempt, extraction
   result and checkpoints. Reacquire admission and the row lock before cleanup.
3. Delete the upload's Qdrant points and Neo4j projection/markers.
4. Delete the source object only when no other upload references its key.
5. Delete the PostgreSQL upload and any now-unused course. Existing foreign
   keys cascade its processing attempts, checkpoints and graph job.
6. Reconcile retained publication intents after committed source deletion.
   Neo4j's deletion fence blocks subsequent marked graph writes, and a retained
   PostgreSQL receipt records successful cleanup.

The admission lock is shared with uploads and retry. Holding it through the
reference check and remote deletion prevents concurrent deletions from both
preserving an orphaned shared object, and prevents a new upload from writing
the same object key while it is being deleted. This intentionally serializes
those operations; queries do not acquire the admission lock.

## Interrupted work

| Interruption | Durable state and recovery |
| --- | --- |
| Before the deletion flag commits | Original READY document and all artifacts remain. No automatic removal is authorized. |
| After the flag, before metadata deletion commits | The existing row records unfinished removal. Queries, preview and retry exclude it. A new process repeats idempotent cleanup. |
| After the metadata commit, before the HTTP response | The document is removed. Retained publication intents remain available to clean a late graph write. |
| After Neo4j tombstone commit, before receipt commit | The existing cross-store recovery helper retries the idempotent tombstone and records one receipt. |

The existing combined-service/worker retention loop resumes up to 50 pending
explicit removals at startup and each configured cleanup interval (default six
hours). This recovery also starts when automatic demo expiry is disabled.
Expiry and public-sample exclusions still apply to automatic expiry; they do
not cancel a removal already explicitly requested. A pure API-role process
depends on its worker running the loop. A user may also repeat DELETE on a
retained pending record.

Queries beginning after the flag commit obtain only eligible READY documents.
Already-running queries that loaded their READY context earlier are not
cancelled. The stored ingestion stage is retained until final removal, so the
existing document list can still show that stage while cleanup is pending;
the course's READY counts exclude the pending upload.

## Verification and limits

The initial reproducer exited immediately after Qdrant deletion and found a
READY document still eligible with zero vectors, two concepts and its source
object. The fixed runner exercises the real authenticated ASGI DELETE route
against disposable PostgreSQL, Qdrant, Neo4j and MinIO. Seven instrumented
boundaries use actual SIGKILL; four additional boundaries inject exceptions.
Fresh recovery processes, concurrent recoverers, concurrent shared-object
deletes, startup initialization, checkpoint cascades and late graph writes are
checked in [the local report](../evaluation/upload-deletion-current.md).

MinIO exercises the S3 client protocol, not hosted Cloudflare R2. These checks
do not prove every network/commit ambiguity, in-flight query cancellation,
full ingestion-worker publication/completion, throughput or hosted capacity.
Discovery is bounded per interval; repeated failures among the oldest 50
pending records can delay later work. Automatic graph-only continuation
remains disabled.

There is no schema rollback to perform. An older application release ignores
the new JSON flag, so drain pending removals before reverting to such a
release. Existing source data, auth and endpoint schemas are unchanged.
