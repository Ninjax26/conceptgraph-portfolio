# Current-binary legacy drain and rollback rehearsal

Scope: Phase 3's next local qualification checkpoint. Production flags, roles,
schema and vendor budgets remain unchanged. This is a coordinated **known cohort**
experiment, not historical-binary qualification or discovery of unknown writers.

1. Add failing contracts for explicit fixture-only scoped/legacy configuration,
   observational stop receipts and strict broad-read snapshot compatibility.
2. Reuse the real Uvicorn API and `app.worker.run_worker` with isolated PostgreSQL,
   Neo4j, Qdrant and MinIO, fixed vectors and authored graph/answer adapters.
3. Produce a READY legacy document. Capture another legacy worker's complete
   vector PUT; close API admission and observe real coordinator shutdown. Verify
   the worker and lease remain until that exact request is acknowledged. Drain
   every known actor before starting the scoped cohort.
4. Recover interrupted work naturally, retain the original READY document/task
   and legacy point IDs, query/preview both, and delete a third source to create
   genuine durable cleanup obligations.
5. Drain the scoped cohort. Deliberately replay known stale fixture points,
   demonstrate broad-read snapshot incompatibility, and reconcile only that known
   execution under admission/source locks. Do not call this automatic recovery.
6. Start a legacy rollback cohort only after a fresh compatible scan. Verify
   source/task preservation, retrieval/preview, deletion consistency and retained
   cleanup obligations. Keep every global activation/drain diagnostic false.
7. Run targeted/full offline tests, real-store qualification, frontend build,
   source-hash/compile/diff checks; obtain one fresh read-only final review.
8. Commit/push only scoped verified code/evidence to origin and verify existing
   free-service CI/revision/readiness/public preview; keep post-release notes local.

Acceptance records must distinguish graceful drain from forced termination,
current code in legacy mode from historical executables, supplied actors from a
complete fleet, local S3 from hosted R2, deterministic adapters from AI quality,
and a fixture controller's rollback decision from runtime enforcement. No hosted
inference, production fixtures, destructive migration or automatic activation.

- [x] Contracts fail before implementation and pass after it.
- [x] Actual cohort transitions and cross-store checks pass.
- [x] Verification, review and exact source hashes recorded.
- [x] Scoped release verified; Phase 3's remaining work documented.

Confirmed additional defect: the first two integration attempts stopped at the
rollback audit because its synthetic payload fixture used `course_id`, whereas
the actual parser stores the canonical course UUID as `document_id`. An actual
parser-produced chunk reproduced this as a failing characterization test. Correct
only the auditor's requested/checked field and its fixtures; preserve parser,
stored payload and API formats. Rerun the full suite and owned-store experiment.

Final verification: 644 backend tests (4.469 seconds), nine frontend tests and
TypeScript/Vite build (Vite 6.44 seconds) pass. The final owned-store run passes
84 grouped checks, including 11 transition checks; 47 recorded source hashes match
and compile. Its 220.181-second elapsed time is not a capacity measurement.
Existing timer waits are 59.137/59.986 seconds for the actual 60-second timer;
their fixture-only clock advances remain distinct from the transition rehearsal,
which uses none. Ten prior hard-kill boundaries remain covered; the transition
cohorts use graceful shutdown only. Owned resources and compile temporaries removed.

The single fresh reviewer identified one important report-command issue: the
printed invocation omitted required selected `--vector-rollout-audit`. Replaying
the omitted-flag command exits 2 at argument validation before resource access.
Two RED→GREEN contracts now cover the complete command and unexecuted modes.
The full suite and owned-store run were repeated after the single review-fix pass.
The reviewer also checked the native parser-field correction and source hashes;
no other blockers were reported. No additional reviewer or implementation agent.
