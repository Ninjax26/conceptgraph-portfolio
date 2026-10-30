# Phase 3: one-claim graph-only processor

The existing checkout was moved from Desktop/conceptgraph_portfolio to
Desktop/DESKTOP_ARCHIVE/conceptgraph_portfolio. Its origin and HEAD still match
the verified 8b883e7 release, including the two local post-release notes. Continue
in that same checkout; preserve all unrelated/personal files and production flags.

Implement a disconnected, default-off processor for exactly one existing graph
claim. Reuse authoritative snapshots/checkpoints, extraction's heartbeat and
durable quota deferral, immutable drafts, the durable publisher's whole-unit
cancellation drain and its receipt/marker recovery. Do not parse/OCR, embed,
write vectors/source objects, rotate READY tasks, or change source eligibility.

1. Add failing offline contracts for flag/role/lease gates, draft-first replay,
   extraction and lock/commit order, quota deferral, lost authority, capacity,
   publication failures and cancellation.
2. Implement the small orchestration layer; acquire admission before document/job
   locks for draft accounting, and release all sessions before remote publishing.
3. Add an owned PostgreSQL/Neo4j qualification using authored extraction and the
   actual snapshot/draft/publication services. Keep automatic dispatch unconnected.
4. Run backend/frontend/build/compile checks, obtain one fresh read-only review,
   fix important findings with RED→GREEN regressions, and record limits honestly.
5. Push only scoped verified code/evidence to origin and verify the existing free
   service revision/readiness/public preview. No hosted AI calls, paid workers,
   production fixtures, migrations or rollout activation.

Acceptance for this increment is the one-claim processor only. Runtime bounded
dispatch, realistic load, provider accounting/priority completeness and versioned
cross-upload cache remain open; Phase 3 is not complete.

Verification ledger: baseline 644 tests pass; 13 new contracts RED→GREEN; final
657 tests (2.419s), nine frontend tests, TS/Vite build (3.16s), 15 actual owned
PostgreSQL/Neo4j checks and 15 matched/compiled source hashes pass. Initial Docker
unavailability failed before qualification; the completed rerun follows starting
the existing local engine. The 60.152s fixture duration is not a capacity metric.
Five prior publisher hard-kill checks remain distinct from processor/worker
recovery. Owned resources and compile temporaries removed; zero hosted calls.
The one fresh read-only reviewer found no important gaps. No rulings/deferred
minors. Scope remains the disconnected one-claim processor, not runtime dispatch.

Release: pending exact commit/CI/deployed revision/readiness/public-preview checks.
