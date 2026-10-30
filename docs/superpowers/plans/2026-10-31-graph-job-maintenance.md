# Phase 3: bounded graph recovery and safe terminal retention

Spec: IMPLEMENTATION_PROGRESS next checkpoint and agreed Phase 3 constraints.
Continue the existing checkout; current release 1d37749 and two local release
notes are preserved. No production activation, hosted inference, new schema,
paid service, external API changes or destructive production operation.

1. RED→GREEN: bounded read-only discovery of exhausted publication intents with
   stable cursor pagination; authority remains the existing fenced reconciler.
2. RED→GREEN: terminal snapshot/draft retention must never discard recovery
   inputs for an intent without a completion receipt. Filter candidates and
   recheck under source/job locks; retain tombstones, checkpoints and receipts.
3. Add separately default-off idle-worker maintenance, rate limited per process,
   serialized per coordinator, with bounded discovery/reconciliation and cleanup.
   Release discovery sessions before remote reads; drain complete cleanup commit
   and existing reconciler on cancellation. Use installation admission first.
4. Offline contracts plus owned PostgreSQL/Neo4j tests: final ambiguous commit
   recovery, absent marker refusal, selection/cursor bounds, retained ambiguous
   old bodies, completed-body cleanup, repeated cancellation and actual runtime
   wiring. Authored fixtures only; disclose shared-event-loop/clock advances.
5. Full backend/frontend/build/hash/diff verification, one fresh read-only final
   review with one regression fix pass if needed, scoped origin push and existing
   free-service revision/readiness/public preview verification.

Pre-flight: retention consumes terminal jobs but final-attempt reconciliation
needs saved snapshot/draft. Ruling: unresolved durable intents protect those
bodies indefinitely; capacity limits apply, no automatic destructive eviction.
Cursor pagination prevents a full oldest-unresolved batch from permanently
hiding later candidates. Sweep cadence is per process, not a fleet-wide lock.
Acceptance is this runtime maintenance increment, not production rollout or
combined/separate OS graph-worker crash qualification. Phase 3 stays partial.

Progress: baseline 668 tests (4.119s); missing module RED and seven new offline
contracts GREEN (0.044s); full suite 675 (7.757s), nine frontend tests/build
(8.48s) pass. Actual old prune discarded recovery snapshot in RED; final owned
PostgreSQL/Neo4j matrix24 and existing PostgreSQL regressions48 pass. An earlier
20-check run predates stronger runtime fixture; final24 is authoritative. All21
report hashes match/compile, legacy hashes match; resources removed, zero hosted
calls. Terminal fixture ages/lease expiry advances disclosed, no capacity or
separate OS graph-worker crash claim. One fresh read-only final review found no important findings, no rulings/deferred
minors or second review. Final suite675 (14.050s), diff checks and all21+18
report source hashes match/compile. Release verification pending.
