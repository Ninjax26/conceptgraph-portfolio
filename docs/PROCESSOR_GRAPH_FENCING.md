# Processor graph fencing — opt-in, not activated

The generation fence is now reachable from the actual document processor under
`GRAPH_PROJECTION_FENCING_ENABLED`, which defaults to false. This setting is
separate from automatic continuation/snapshot staging. No production environment
or Render configuration is changed by this implementation.

The legacy race was a check/write gap: the processor relied on a heartbeat event,
then separately deleted and stored Neo4j data without advancing the upload fence.
An older marked write could replace a newer retry. Failure cleanup also checked
ownership before remote cleanup, releasing its protection before deletion. Finally,
ordinary deletion removed graph data but did not permanently close a baseline
upload without a publication intent.

With the opt-in setting:

- `_with_current_attempt` obtains a fresh PostgreSQL document/task/lease-owner
  row lock and rejects non-active or removal-pending sources. It retains that
  lock while the I/O unit drains, including repeated cancellation. Existing
  upload ownership and stage/failure commit methods remain authoritative.
- Initial vector cleanup and vector writes run under that source fence. Graph
  replacement uses one Neo4j transaction and the locked **upload attempt count**;
  it does not mint a graph-job claim or rotate the source task token.
- Failure cleanup closes the graph attempt before deleting vectors filtered by
  both upload and execution token. It marks failure under the same source lock,
  using the existing error classification and retry policy. A stale task cannot
  enter cleanup. Source PDFs and extraction checkpoints remain available for retry.
- The authenticated deletion route and retention/recovery sweep use a permanent
  Neo4j tombstone after the irrevocable pending-removal flag is committed. This
  prevents late generation-fenced writes even when there is no publication intent.
  Metadata removal still waits for vector, graph and shared-object cleanup. A
  failed/cancelled removal remains pending and excluded from READY retrieval.

The default-off path retains its existing writer/cleanup behavior. Public API
schemas, authentication, IDs, source storage keys, ingestion stages and graph
continuation dispatch are unchanged. Neither setting is enabled by this change.

## Local verification

The [four-store report](../evaluation/processor-fencing-current.md) uses the real
processor, native PyMuPDF parsing, actual Qdrant point writes, Neo4j transactions,
PostgreSQL state transitions and MinIO source objects. A fixed three-dimensional
encoder and authored graph output replace AI inference; no model is downloaded
or called. The protected DELETE endpoint is exercised with an ASGI client.

Seven processor checks cover READY state and current-task vectors, an actual
manual retry rejecting old graph replay, stale source cleanup, failure after
vector commit, deletion without an intent, repeated cancellation while retaining
the document lock, real attempt recovery, and removal of the remaining fixtures.
The separate 18-check deletion matrix covers seven SIGKILL boundaries, four
exception boundaries, restarted recovery, authentication, eligibility and shared
source/neighbor preservation. The opt-in metadata-commit case now verifies that
the tombstone blocks the old write before reconciliation, rather than first
allowing it to recreate data.

These are mechanical consistency checks. Graceful processor cancellation does
not establish processor hard-kill safety. Late Qdrant writes after a killed
process, pre-rollout unfenced writers, hosted R2, actual split-worker operation,
semantic quality and capacity remain unqualified. Do not activate the setting or
automatic continuation based only on this report. Phase 3 is still partial.

Mixed configurations are unsafe: enabling only workers while API/retention
deletion still uses the legacy path bypasses the permanent tombstone contract.
Activation therefore requires a consistent rollout and a drain policy for older
writers. The next investigation should replay an old Qdrant point after a newer
attempt: point IDs still derive from `chunk.id`, while task ownership is stored
in payload. Characterize that ordering and every reference/cleanup path before
changing IDs. The current report does not claim that race is solved.
