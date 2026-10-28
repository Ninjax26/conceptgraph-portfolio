# Graph projection replacement — internal primitive, not activated

Phase 3 increment, 14 October 2026.

`IngestionService.replace_graph_extraction` replaces one upload's concepts and
relationships inside **one Neo4j managed write transaction**. The internal marked
publisher calls it; the live API, coordinator and ingestion processor do not.
Existing uploads retain the original pipeline. Automatic continuation and
snapshot staging remain disabled.

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

## Optional remote commit marker — 25 October 2026

The same managed Neo4j transaction now optionally writes a `GraphPublication`
marker after replacing the upload projection. Its fields are the intent, job,
upload, course, original READY task and validated candidate digest. Identity
mismatch is rejected before opening a session; a conflicting existing marker
rolls back the replacement. A separate reader looks up the marker by exact
intent ID. No API, worker or coordinator calls either operation. Existing graph
writes omit the marker and keep their previous query sequence. The marker is not
a PostgreSQL receipt, an authorization token or a complete cross-store recovery
protocol. The future caller must tie it to a committed intent and current fence,
then reconcile commits, deletions and hard kills before dispatch is enabled.

The current offline suite has 523 passing backend tests, including five new
marker boundary checks, plus nine frontend tests, TypeScript/Vite build and
compile/diff checks. The isolated real Neo4j runner passes nine checks,
including atomic marker commit, exact read, replay and conflicting-identity
rollback; it removes its disposable container and network. No hosted provider
call or production test-data mutation was made. See the refreshed
`evaluation/graph-projection-current.md` for the test limits and source hashes.

No parsing, OCR, embedding, vector deletion, schema/environment change, provider
call or production test-data mutation is performed by this increment.

## Generation-compatible baselines and attempt cleanup — 28 October

The optional `source_generation` argument now gives an initial/retry projection
the same remote upload fence as marked continuation. Supply the positive
**upload attempt count**, not the graph-job claim count, and the original source
task token. It cannot be combined with a publication marker. With neither
argument, the legacy replacement query sequence is unchanged.

- A higher generation may replace an older projection. At the same generation,
  a baseline replay must retain the exact task and canonical graph payload hash.
- That baseline can advance to a marked publication in the same task/generation.
  A delayed baseline cannot then overwrite or abort the published graph. A
  different marked payload remains rejected, as before.
- Baseline graph replacement, old-marker removal and generation advancement
  commit together. Empty graphs still advance the fence. Failed writes roll
  back the generation and graph together.
- Internal `cleanup_graph_attempt` closes a failed baseline generation and
  deletes its graph/markers in the same transaction. It rejects an older or
  foreign task and a same-generation marked publication. Late writes to the
  aborted attempt remain blocked, including when cleanup precedes the first
  write. A higher generation can retry. Permanent deletion blocks every later
  generation, including baselines and attempt cleanup.

Both operations use the existing unique upload constraint and drain their
managed transaction on cancellation. The existing fence gains optional
`baseline_sha256` and `aborted` properties when used; old marked fences need no
PostgreSQL migration or data rewrite. Task/source authority must still be proven
by the caller in PostgreSQL. The remote fence alone is not authorization or a
distributed transaction.

The [new real-store report](../evaluation/graph-baseline-projection-current.md)
records fourteen new ordering checks and nineteen rerun projection regressions
against owned disposable Neo4j 5.26.26. New checks include both concurrent
baseline/publication orderings, cleanup waiting for an in-flight baseline,
rollback after real deletion, empty graphs and other-upload/course preservation.
The existing regression matrix supplies managed-retry and cancellation coverage.
Seven new offline tests initially failed because these entry points were absent;
the complete backend suite now passes 564 tests. No PostgreSQL, Qdrant, R2, live
API, models or complete ingestion-worker process is tested by this matrix.

**Live integration remains pending.** The processor still calls legacy unmarked
`cleanup_graph` and `store_graph_extraction`; failure cleanup and vector cleanup
are unchanged. A separate replay probe again observed the newer legacy task's
concept count fall from four to zero. Do not call this an end-to-end retry fix,
enable dispatch or treat that unsafe observation as a safety pass. Integrate and
qualify ordinary/compensating cleanup with source authority and real processor
ordering next. Automatic continuation remains disabled.

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
