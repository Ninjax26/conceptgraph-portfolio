# Graph projection replacement — internal primitive, not activated

Phase 3 increment, 14 October 2026.

`IngestionService.replace_graph_extraction` replaces one upload's concepts and
relationships inside **one Neo4j managed write transaction**. It is not called
by the API, coordinator, ingestion processor or graph-job service yet. Existing
uploads retain the original pipeline. Automatic continuation and snapshot staging
remain disabled.

## Implementation

- Delete by both canonical course and upload identity, write the replacement,
  then remove the course only if it has no remaining concepts. Other uploads are
  not selected for deletion.
- Reuse the existing node/relationship writer and scoped IDs, six permitted
  relationship types, source fields and original READY execution token. The
  existing auto-commit method retains its query order and parameters.
- Validate identity, structure and bound primary provenance before opening a
  session. Reject unsupported edges and normalization that silently drops nodes
  or edges. Provider batch limits do not apply to the merged document graph.
- Neo4j `execute_write` commits/rolls back and retries the whole callback.
  Replacement is idempotent for content; course `updated_at` can advance.
  Deletion and insertion are not independently committed or retried.
- `run_async_safely` shields and drains the session/write on cancellation,
  including repeated cancellation, then propagates cancellation. The existing
  thread-write guard delegates to the same drain logic.

## Required integration and limits

The caller must own the fresh PostgreSQL **document → job** fence and verify
snapshot/task/claim/source eligibility. This primitive cannot certify that fence;
nonblank provenance does not prove that source objects exist or that relationships
are semantically correct.

Draining can finish a remote commit before cancellation is propagated. A future
publication service must shield the **whole fenced publication/completion unit**,
not merely call this primitive then roll back PostgreSQL on cancellation.
Neo4j and PostgreSQL do not share a distributed transaction. Commit ambiguity,
process death between commits, reconciliation and repeatable completion still
need implementation and tests. No graph-only completion or dispatcher was added.

No parsing, OCR, embedding, vector deletion, schema/environment change, provider
call or production test-data mutation is performed by this increment.

## Saved-candidate replay validation (internal, not dispatched)

`graph_publication_candidate.restore_graph_publication` checks a saved candidate's
exact JSON checksum, validates its graph, then rebuilds the canonical candidate
against independently supplied current chunks and document identity. Changed
source text/metadata or identity, malformed/coerced payloads and extra envelope
fields fail closed. Restored output is independent of the saved JSON.

The caller must obtain those inputs under the current document/job fence; trusting
identity or chunks supplied by the saved payload would defeat the check. A checksum
does not authenticate an author or establish graph correctness. This pure helper
does not persist candidates, hold locks, publish graphs, complete jobs or reconcile
ambiguous remote commits. There is no replay worker or activation change.

## Initial offline verification — c29706a

Ten new tests use an **authored managed-transaction double**, not a Neo4j server.
They protect the one-callback query boundary, scope parameters and original writer
parameters; failure propagation at each of six writes; replayed content; a
13-node/12-edge aggregate; explicit empty cleanup; pre-session rejection of
foreign/missing provenance, empty identity, invalid endpoints/types and silent
normalization loss; repeated-cancellation draining; and a remote failure during
draining preserving cancellation.

| Command | Result |
| --- | --- |
| `.venv/bin/python -m unittest tests.test_graph_projection_replacement -v` | 10 passed |
| `.venv/bin/python -m unittest tests.test_processing tests.test_scaling_operations -q` | 159 passed |
| `.venv/bin/python -m unittest discover -s tests -q` | 439 passed |
| `npm run test:frontend` | 9 passed |
| `npm run build` | TypeScript check and Vite build passed |
| `.venv/bin/python -m compileall -q app tests scripts` | Passed |
| `git diff --check` | Passed |
| `docker info --format '{{.ServerVersion}}'` | Could not connect to local daemon |

No daemon was started or packages installed in that initial run. Actual Neo4j
rollback, other-upload preservation and managed retry had not yet been verified;
the subsequent isolated run below checks those boundaries. Cross-store crash
behavior remains unverified. The double is a call-boundary check, not an atomicity,
capacity, semantic accuracy or production recovery measurement. Earlier PostgreSQL
reports were not rerun this increment; their hashes remain historical evidence
for their releases. Deployment checks are recorded separately after pushing.

## Subsequent isolated Neo4j verification — 14 October 2026

[Seven checks](../evaluation/graph-projection-current.md) now pass against a real
Neo4j **5.26.26** server using the installed official image, with source/image
hashes recorded in the adjacent JSON. Reproduce with:

```bash
.venv/bin/python scripts/evaluate_graph_projection.py
```

The runner creates its own labelled Docker network and container, publishes Bolt
only on a random loopback port, disables usage reporting, uses memory-backed data
and no host volume, and removes its exact created IDs afterward. It does not
accept an existing database URL or access existing application containers.

- A merged 13-node/12-edge projection retains original READY task scope and
  preserves other uploads' concepts, relationships and containment links.
- Repeating replacement creates no duplicate content or edges.
- Real server state rolls back after both an injected callback exception and an
  actual invalid-Cypher error following deletion/uncommitted insertion.
- The actual driver retries the entire managed callback after one injected
  `ServiceUnavailable`; this is not a measured network outage.
- Repeated cancellation waits for the actual transaction to settle before
  propagating. The fully committed replacement is then readable.
- Empty replacement preserves a nonempty shared course and removes only a course
  left empty after its last upload is removed.

The first test-only network configuration did not expose a port, and an initial
256 MiB memory-backed database exhausted space during transaction-log
preallocation. These were harness failures, not application failures. The runner
was corrected to use its own ordinary Docker network, retain startup diagnostics,
clean up even after failures, and bound test-only transaction-log allocation.
No production configuration changed. Docker Desktop was started; its saved
restart policies also started existing user containers, which were left untouched.

All 439 backend tests, nine frontend tests, TypeScript/Vite build, compile and diff
checks pass again. No hosted AI calls, image pulls, paid services, environment
edits or production test-data writes were made. The seven checks use authored
graphs/faults and do **not** verify PostgreSQL job fences, Qdrant/R2 consistency,
hard-kill recovery, ambiguous commits between stores, semantic quality or scale.
The primitive remains disconnected from processing/dispatch and defaults stay off.
