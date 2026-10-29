# Read-only vector rollout preflight implementation plan

> **For agentic workers:** Use superpowers:executing-plans inline, TDD and one fresh-context final review.

**Goal:** Identify incompatible READY vector payloads and API/worker vector-contract drift before any opt-in rollout.

**Architecture:** Add an unscheduled read-only auditor with injected clients and explicit immutable source/peer contracts. Reuse the existing endpoint/collection fingerprint and deletion policy. Scroll only selected metadata, in bounded pages; never embed, repair, delete, relabel tasks or activate flags. Qualify it inside the owned four-store harness.

**Spec:** `docs/IMPLEMENTATION_PROGRESS.md` current Phase 3 checkpoint and `2026-10-28-vector-execution-scope.md` activation policy. This is one bounded prerequisite, not a fleet-drain certificate or complete deployment compatibility check.

**Global constraints:** Offline inference only; isolated stores; no production data/environment changes, new dependencies, API/schema changes or paid resources. Keep both rollout flags disabled in production and preserve existing local release notes. Results contain counts/error types/digests, never text, filenames, credentials or endpoint URLs.

**Ruling:** Count-only inspection cannot distinguish current from stale/untagged vectors or duplicate chunks. A write-capable migration risks relabelling stale evidence. Choose bounded metadata inspection instead; leave mutations and actual legacy-writer drain outside this task. Equal vector contracts do not prove equal PostgreSQL/Neo4j/R2 configuration or physical store identity.

## Review focus

- Contract mismatch, missing/invalid source identity and malformed pagination fail closed before claiming coverage.
- Matching legacy physical IDs remain compatible when their current execution payload is valid; duplicate logical chunks are detected.
- Unknown/untagged tasks cannot become current merely because a total count matches.
- Truncation, RPC errors, cancelled reads and concurrent writers cannot turn a partial scan into operational approval.
- No automatic use or broad cleanup is added to running services.

## Task 1 — Auditor and isolated qualification

- [x] RED: tests for contracts, eligibility, current/legacy IDs, duplicate/stale/unknown/untagged payloads, projection and bounds/error/cursor handling. Expected: missing auditor import fails.
- [x] GREEN: implement `app/services/vector_rollout_audit.py`, using `run_blocking_safely`; no store mutation or activation path. Expected: focused contracts pass.
- [x] Add `scripts/vector_rollout_checks.py` and optional `--vector-rollout-audit` harness mode/report. Expected: actual Qdrant pagination/projection and PostgreSQL attempt-history fixtures detect the incompatible cases; metadata/vector state stays unchanged.
- [x] Run isolated qualification, full backend suite, frontend/build, compile/diff/hash checks. Expected: pass with owned resource removal and zero hosted calls; record actual counts.
- [x] Fresh review/regression, scoped commits/push and exact deployed API revision/CI/public-preview verification are complete. Final release: `8d252989b4a146f31279894ef4ee801aebb209fd`; Verify `40821468064` passed. No activation was performed.

Local verification complete: combined report passes 68 checks, including 10 vector-audit cases and 1 controlled current-schema startup case, with 41 matching source hashes/compile checks and owned resource removal. 621 backend tests, nine frontend tests, TypeScript/Vite build and diff pass. Separate legacy provider-budget PostgreSQL suite passes 10 checks. Auditor review found malformed direct contract construction; its RED/GREEN regression and strict validation fix are included. A separate startup-fix review found no blockers. No activation, hosted AI or production test fixture. Release verification is recorded separately in the main ledger.
