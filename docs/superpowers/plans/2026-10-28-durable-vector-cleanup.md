# Durable late-vector deletion cleanup

Use superpowers:executing-plans inline, with one final fresh-context review.

**Goal:** Repair the measured post-delete vector orphan without activating either rollout flag or publishing an unqualified change.

**Architecture:** An additive PostgreSQL `vector_deletion_tombstones` table retains upload/task/store identity without cascading foreign keys or document content. Opt-in deletion stages every known attempt plus the current task atomically with the pending-removal flag, before remote I/O; existing admission/source locks are reacquired after commit. The normal DELETE still removes current artifacts and the source as before. A bounded background sweep checks retained records fairly using database-clock due times, source absence, exact configured store binding, admission/row locks, and exact upload+task Qdrant filters. An empty or acknowledged cleanup reschedules the record; it never retires it as proof that all delayed writes have finished. Failure also reschedules without dropping authority. Cancellation drains the session/I/O unit before releasing locks. Source recreation is fail-closed; task filters additionally protect a newer task if a cleanup request itself outlives its process.

**Constraints:** Keep `GRAPH_PROJECTION_FENCING_ENABLED=false` and `GRAPH_CONTINUATION_SNAPSHOTS_ENABLED=false`. Offline inference substitutes and isolated stores only. No production data or environment changes. No push/deploy until qualification passes. The new schema is additive/reversible and must survive initialization, reinitialization and source deletion locally. No automatic tombstone expiry: retirement requires a separately proven writer-drain policy. Endpoint/collection changes must not redirect a stored cleanup obligation to another store.

## Steps

- [x] Ten focused RED/GREEN contracts cover durable schema/no cascade, scope binding, atomic staging, guards, task-filtered cleanup, retained checks, bounded discovery and default-off no work.
- [x] Add table/service and opt-in deletion/retention integration; preserve existing APIs and default paths.
- [x] Real worker experiment passes after an earlier empty fresh-process sweep and after a subsequent explicit point replay. Only owned fixture due times are advanced; no manual cleanup satisfies acceptance.
- [x] Twelve real-store durability/isolation checks include staging rollback, restart/cascade survival, concurrent cleanup, store/source/task/neighbor guards, lost acknowledgement, cancellation drain and source recreation. Empty-table rollback/reinitialization leaves existing data unchanged; populated obligations must be retained on code rollback.
- [x] 601 offline backend tests, 50 real-store checks, nine SIGKILLs, 27 matching source hashes/compile and diff checks pass; owned resources removed. Frontend unchanged, so no new frontend build is claimed.
- [x] One fresh read-only review found no scoped blockers. Both rollout flags remain disabled. Changes stay local; no commit/push/deploy this increment.

Result: [vector-deletion-recovery-current.md](../../../evaluation/vector-deletion-recovery-current.md) is PASS; prior [worker-crash-current.md](../../../evaluation/worker-crash-current.md) preserves the original FAIL. The first durability-fixture run correctly rescheduled a live-source check; its later assertion needed a fixture-only due-time advance. Runtime cooldown was preserved rather than weakened. Final report records zero hosted calls/production test-data mutations and no manual recovery credit. Remaining full-worker/timer/legacy/config-migration gates still do not establish activation readiness or full Phase 3 completion.

The guarantee is repeatable eventual cleanup after a sweep, not a cross-store transaction or instantaneous rejection of a previously accepted Qdrant request. Private workspaces, hosted R2, mixed legacy writers, config migration, the complete worker lifecycle and full Phase 3 remain separate gates.
