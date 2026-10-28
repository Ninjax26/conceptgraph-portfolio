# Opt-in vector execution isolation

Use superpowers:executing-plans inline, with one final fresh-context review.

## Design and ruling

The previous processor fence protects entry into writes, but cannot drain I/O after a killed process. Current Qdrant IDs depend only on the logical chunk ID, so replaying an old request can overwrite a newer execution. Retrieval currently filters upload IDs, not exact READY upload/task pairs. Changing IDs alone would retain stale candidates and therefore is insufficient.

Ruling: address both boundaries behind the existing default-off projection gate. Use a distinct versioned UUID namespace and an unambiguous upload/task/chunk tuple for physical vector IDs. Retain logical chunk metadata and citation URLs. Derive read scopes from eligible PostgreSQL records, validate them before embedding admission/I/O, and apply exact upload/task pairs in dense, older-client fallback and identifier searches before candidate limits. Existing UUID points with matching execution payloads remain readable; untagged points fail closed in opt-in mode. Default-off writes and upload-only reads retain their original behavior. No schema, environment or production data migration occurs.

Alternatives rejected: an ID-only change admits stale vectors; client-side filtering alone lets stale vectors consume bounded search slots; rewriting all production vectors would need a migration and is unnecessary for this gated qualification.

## Work

- [x] Audit producers, retrieval filters, candidate identities, citations and cleanup. No direct point-ID lookup depends on the legacy physical UUID; citations use document/page/passage.
- [x] Add failing offline regressions; reproduce the old request replay in the actual private four-store processor fixture before changing runtime code.
- [x] Implement versioned point IDs and exact paired READY scopes without changing the default path. Index execution tokens for opt-in writes; validate reads before embedding admission.
- [x] Verify an old point replay cannot overwrite the current execution, stale/cross-paired/missing-task points cannot occupy dense/identifier slots, and legacy UUID citations remain compatible.
- [x] Four-store matrix: 10 processor plus 18 deletion checks, seven deletion SIGKILL boundaries; 583 backend tests, nine frontend tests, TypeScript/Vite build, five-file compile/diff and 18 matching report hashes. Owned resources removed; zero hosted calls.
- [x] One fresh read-only review found no scoped blockers; open activation gates were explicitly excluded from the verdict.
- [ ] Scoped commit/push and existing free-service revision/readiness/public-preview verification.

## Limits and activation policy

This controlled replay is not a worker SIGKILL/network-delay experiment. Full ingestion process death and recovery remain open. Task-specific IDs preserve newer evidence but cannot prevent a delayed request from creating old orphan vectors after deletion; durable reconciliation remains required. Neo4j query scopes are unchanged. Do not claim cross-store atomicity, physical deletion safety for every late request, semantic accuracy, hosted capacity or Phase 3 completion.

Keep `GRAPH_PROJECTION_FENCING_ENABLED=false` and `GRAPH_CONTINUATION_SNAPSHOTS_ENABLED=false`. Before any future activation: drain older API/worker/helper writers and outstanding requests; verify current READY task payload coverage or explicitly reprocess incompatible uploads; qualify hard-kill recovery and late-write deletion reconciliation. Do not relabel unknown legacy vector payloads as current. Older binaries can ignore new UUID points, but rollback to upload-only queries requires draining scoped writers and cleaning stale executions first. No in-place mixed-writer rollout is claimed safe.
