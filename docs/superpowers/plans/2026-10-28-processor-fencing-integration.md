# Opt-in processor fencing integration

> Use superpowers:executing-plans inline, with one fresh-context final review.

**Goal:** Exercise the generation fence through the actual document processor and deletion routes without activating it in production.

**Architecture:** A separate default-false `GRAPH_PROJECTION_FENCING_ENABLED` setting gates the integration. Retain the existing PostgreSQL source/task/owner row lock through drained vector/graph I/O. Replace graph cleanup/write pairs atomically with the source upload attempt generation. Failure cleanup closes that attempt and filters vector deletion by its execution token. Durable pending document removal permanently tombstones the graph, including baselines without an intent.

**Constraints:** No hosted inference/embedding/reranking, private PDFs, production test data, new dependencies, schema/environment-file changes or paid services. Do not activate automatic continuation or projection fencing. Preserve original task tokens, stages, retry counters/classifications, checkpoints, READY eligibility and deletion recovery.

**Ruling:** Reuse `UploadService._get_current_upload` ownership semantics rather than inventing a different lease-expiry rule. It locks current task/owner; a claim/recovery that changes those values fences the old process. An existing owner's renewal policy is unchanged.

- [x] Write failing offline tests for the default-off gate, atomic processor replacement, stale-source refusal, failure cleanup, permanent deletion and task-filtered vector cleanup.
- [x] Integrate the opt-in processor plus API/retention deletion; retain unchanged default behavior.
- [x] Run the actual processor with real private PostgreSQL/Qdrant/Neo4j/MinIO, native synthetic PDF parsing, authored graph output and a fixed three-value encoder. Verify retry/replay, source fencing, failed vector acknowledgements, cancellation/drain/recovery and deletion without an intent.
- [x] Extend the existing isolated deletion matrix under the fixture-only flag; preserve the older default-mode report separately. Verify seven deletion SIGKILL boundaries and four injected exception boundaries.
- [x] Full backend suite: 570 tests pass; the first real fixture rejected its mismatched dimension setting, which was corrected to three without changing runtime validation.
- [x] One fresh read-only review found no scoped correctness blockers; qualify old docstrings for default/opt-in callers.
- [x] Finish final store report, nine frontend tests/build, compile/diff and 14 current hash checks; all own disposable resources were removed.
- [x] Commit/push only scoped verified code and verify exact free-service revision/readiness/public preview.

## Acceptance still open

- Processor cancellation here is graceful/drained. It does not prove ingestion worker SIGKILL recovery or late Qdrant write ordering after process death.
- Legacy pre-rollout unmarked writers and out-of-band legacy helpers require a drain/compatibility policy before activation.
- MinIO proves local S3 operations, not hosted R2. Synthetic vectors/graphs prove storage boundaries, not AI/OCR quality or load capacity.
- Keep both `GRAPH_PROJECTION_FENCING_ENABLED=false` and `GRAPH_CONTINUATION_SNAPSHOTS_ENABLED=false` in production. Broader Phase 3 dispatch, accounting, split-worker and cache gates remain open.

Release verified: `32deae61fe43ea62f90e7a3f6f7862ef8d564db4`; CI run `40702763635` passed, API readiness reported that exact revision and prepared frontend/API preview returned 200. The integration and continuation gates were not activated. Remaining hard-kill/Qdrant/legacy-rollout gates remain open.
