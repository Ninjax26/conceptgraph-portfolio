# Generation-compatible graph projections implementation plan

> **For agentic workers:** Use superpowers:executing-plans to implement this plan inline, with one final fresh-context review.

**Goal:** Prove a common remote fence for initial/retry projections, marked continuation and attempt cleanup before integrating it into the live processor.

**Architecture:** Extend the existing atomic replacement with an optional positive source generation. A baseline projection may become a marked publication in the same source task/generation; it cannot replace that publication afterward. Attempt cleanup closes only its generation, while permanent deletion blocks every later generation. PostgreSQL source/job authority remains the caller's responsibility.

**Tech stack:** Existing Python, Neo4j managed transactions, unittest and disposable local Docker fixtures. No dependencies, PostgreSQL migration, hosted calls or feature activation.

**Spec:** `docs/IMPLEMENTATION_PROGRESS.md`, Phase 3 retry ordering gate; `evaluation/graph-retry-final-attempt-current.json` records the failing legacy ordering.

## Constraints and review focus

- Preserve legacy query behavior when neither marker nor source generation is supplied. No live processor/coordinator wiring in this increment.
- Preserve deletion tombstones, original task provenance, transaction rollback/retry and cancellation drain.
- Reject lower generations, same-generation foreign tasks, changed baseline payloads and closed-generation writes before deleting graph data.
- Permit a same-generation baseline → marked transition; reject the opposite direction and conflicting marked payloads.
- Verify an older cleanup cannot erase a newer retry or an already marked same-generation publication. A new generation may retry an aborted attempt; permanent deletion stays closed.
- Tests prove storage ordering, not semantic accuracy, worker capacity or cross-store atomicity. Legacy unfenced writers/cleanup still block activation until actual-processor integration is qualified.

## Task 1 — Common fence and characterization

- [x] Add failing offline tests in `tests/test_graph_baseline_projection.py` for baseline identity/generation, atomic replacement/rollback and attempt cleanup.
- [x] Add `source_generation` to `IngestionService.replace_graph_extraction` in `app/services/ingestion_service.py`, sharing the fence transition with the existing marked path.
- [x] Add internal `cleanup_graph_attempt` using that fence in the same transaction as graph/marker cleanup; leave live `cleanup_graph`/vector deletion unchanged.
- [x] Run focused and full backend tests, then fix only confirmed regressions.

## Task 2 — Real-store ordering and release gate

- [x] Create `scripts/evaluate_graph_baseline_projection.py` with only an owned, loopback Neo4j container/network and synthetic graphs. Verify late replay, both concurrent orderings, abort/retry, permanent deletion, rollback, cancellation and neighbor preservation.
- [x] Rerun the existing projection matrix because the marked fence implementation changed. Keep old retry/final-attempt evidence historical; its ordinary writer remains legacy and unsafe.
- [x] Record actual commands/check counts/source hashes in `evaluation/graph-baseline-projection-current.*` and the implementation ledger.
- [x] Run frontend tests/build, compile/diff/hash checks and one scoped code review.
- [x] Commit/push only verified code to origin, verify exact CI/API revision and prepared preview on existing free services.
- [x] Keep automatic continuation disabled. Next checkpoint is actual processor integration, including ordinary and compensating cleanup, before any dispatch activation.

## Verified checkpoint

Fourteen new Neo4j ordering checks and nineteen legacy projection regressions passed. The separate unfenced replay remains unsafe (four concepts to zero). Seven new unit tests and the full 564-test backend suite pass; nine frontend tests/build pass. One fresh-context scoped review found no blockers. Source authority/actual processor integration remains deferred; this plan does not authorize activation.

Release verified: `0df391ca32aba1ed3c7bcb67176bc10d9fa5b860`; CI run `40667339288` passed, API readiness reports that exact revision and the prepared frontend/API preview returned 200. Actual processor/compensating-cleanup qualification and activation remain outside this completed primitive plan.
