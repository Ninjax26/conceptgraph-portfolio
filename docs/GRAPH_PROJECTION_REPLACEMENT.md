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

## Verification

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

No daemon was started or packages installed. **Actual Neo4j rollback, other-upload
preservation, managed transient retry and cross-store crash behavior remain
unverified**. The double is a call-boundary regression check, not an atomicity,
capacity, semantic accuracy or production recovery measurement. Earlier PostgreSQL
reports were not rerun this increment; their hashes remain historical evidence
for their releases. Deployment checks are recorded separately after pushing.
