# Read-only process inventory checkpoint

Use executing-plans inline, TDD and one fresh-context final review.

**Goal:** Collect and compare fresh configuration/observed namespace evidence from actual separate API/worker processes before any rollout or drain procedure.

**Spec:** Current Phase 3 next-checkpoint in `docs/IMPLEMENTATION_PROGRESS.md`; both rollout flags stay disabled in production. No private workspaces, new dependencies, schema/API changes, production test data or hosted AI calls.

**Ruling:** Add an unscheduled read-only collector using injected existing clients: a dedicated READ ONLY PostgreSQL transaction, Neo4j database-info read, Qdrant collection-schema read and S3 HEAD bucket. Do not use storage readiness helpers that may create buckets. Export only digests, roles/PIDs, timestamps and error types. Compare exactly validated manifests, reject stale/partial/role/config/code differences, and always leave activation/fleet-completeness/writer-drain unverified. URL/schema observations do not prove physical store identity or equal permissions. An operator-run collector does not discover unknown processes.

Actual local API/worker fixture processes will optionally export their own inventories, including their effective private resource settings, before starting the unchanged application lifecycle. Verify compatible peers and independently differing PostgreSQL, graph, vector, bucket/budget/code settings; no environment file changes. This checkpoint does not implement or claim the later coordinated drain/rollback rehearsal.

- [x] RED/GREEN `tests/test_rollout_inventory.py`: metadata-only reads, credential-free digests, unavailable resources, no bucket creation, strict manifests, drift, stale/partial instances, no activation/drain claims.
- [x] Implement `app/services/rollout_inventory.py`, wire optional inventory export/comparison into the actual private API/worker lifecycle, and add an isolated report mode. Preserve normal services and old fixture modes.
- [x] Run actual separate-process/four-store qualification with offline adapters, full tests, frontend/build, source-hash/compile/diff checks; record true counts/limits and owned cleanup.
- [x] Fresh review and four RED/GREEN corrections completed; scoped release `f2a2bf1836a9c0dcb03bf72c1488d17609cb565b`, Verify `40891025997`, exact live API revision/readiness and public preview verified. Phase 3 remains partial; no activation performed.

Verification: 73 real-store/lifecycle checks, ten SIGKILLs, 634 backend tests, nine frontend tests, TypeScript/Vite build, 44 exact source hashes/compile and diff pass. Owned resources removed, zero hosted AI or production fixtures. Fresh review's four P2 diagnostic findings were reproduced and fixed in one pass: instance identity, PostgreSQL query precedence, Unix-socket path case and invalid ports. The actual two peer exports agree; four controller namespace changes do not. Full fleet/drain/activation remain false. Finish and record scoped release verification separately.
