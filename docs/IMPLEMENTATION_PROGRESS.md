# ConceptGraph implementation and verification ledger

Updated: 2026-10-14 (India). This is the current roadmap; older numbered scaling phases in the handbook describe earlier work.

## Operating boundaries

- Continue in small, tested groups. Resume an unfinished phase rather than advancing merely because a scheduled run ended.
- The user selected offline checks only. Do not make hosted LLM, embedding or reranking test calls without a separately approved quota budget. Do not manufacture semantic scores or human annotations.
- Use isolated local data for worker, hard-kill, cleanup and benchmark tests. Never delete, rebuild or reassign production uploads for testing.
- Keep the existing free Render API/frontend and combined ingestion role. Separate workers are tested locally; paid Render workers are not authorized.
- Preserve auth, READY eligibility, leases/task-token fencing, advisory locks, transaction boundaries, checkpoints, deletion and compensating cross-store cleanup. Private workspaces remain deferred.
- Commit and push only scoped verified work to `origin` (`Ninjax26/conceptgraph-portfolio`), never upstream. Keep secrets, personal interview notes, private PDFs and human reviewer information local.
- Deploy only after tests/builds pass. Check the actual API revision, readiness and frontend routes. A healthy readiness endpoint is not an answer-quality or load test.
- Reversible schema changes need isolated migration/restart tests before release; destructive changes or paid infrastructure require user approval.

## Current checkpoint

| Phase | State | Evidence / next action |
| --- | --- | --- |
| 1 — Evidence selection | Integrated; known gaps remain | [Integration report](../evaluation/phase1-integration.md). Original 31/35 source completeness, 10/10 gate refusals; new authored comparison holdout 4/4 and 2/2 across 10/50/100 PDFs. Known missing-attribute cases and four original source misses remain. |
| 2 — Offline quality evaluation | Tooling verified; semantic review pending | [Quality report](../evaluation/quality-evaluation.md). 12 deterministic fallback captures, 20 valid citation indices, 21 literal excerpts, six known snapshot links. No fresh model generation; 12 answers and 44 graph edges are unreviewed. |
| 3 — Provider-safe ingestion | Partial: section/recovery checkpoint fences verified; defaults off | [Recovery checkpoints](GRAPH_RECOVERY_CHECKPOINTS.md) now share exact request identity with extraction and enforce the graph-job fence; nineteen PostgreSQL job checks and seven new offline tests pass. All 429 backend tests pass; twenty compatibility, nine staging and ten manual-retry checks were rerun. [Atomic staging](GRAPH_SNAPSHOT_STAGING.md), [foundation](GRAPH_CONTINUATION_JOBS.md) and admission remain integrated. Next: graph-only processor, atomic projection publication/completion, snapshot accounting and bounded dispatch. No automatic production continuation or snapshot staging is enabled. Hosted-provider admission/actual tokens, full split-worker/cross-store hard-kill tests and cross-upload cache remain unfinished. |
| 4 — Realistic scaling | Pending | Validate separate-process behavior with varied local fixtures and hard-kill recovery; disclose all doubles. |
| 5 — Profile-guided storage/search | Pending | Measure first, then optimize actual filter/index/write/traversal bottlenecks. |
| 6 — Document understanding | Pending | Coordinates/order/tables, uncertainty review/corrections and human-reference OCR tests. |
| 7 — Revision comparison | Pending | Metadata, section alignment, deterministic differences, cited summaries and review. |
| 8 — Repeatable semantic evaluation | Pending human review | Extend Phase 2 foundation; do not confuse mechanical checks with reviewed correctness. |

## Phase 3 — Scale ingestion without overwhelming providers

- [ ] Run the API and workers as genuinely separate local processes; verify shared claiming and worker presence.
- [x] Implement and test optional interactive request/token holdbacks across graph failover/repair; production activation is deferred until delayed scheduling is implemented.
- [ ] Durably defer quota-limited graph work until eligible; avoid hot retry loops and unlimited attempts.
- Manual retry eligibility is now persisted/enforced and tested across a fresh process. Automatic delayed work dispatch remains unimplemented; keep the above acceptance item open.
- Durable graph-job metadata supports independent READY-safe claims, snapshot/contract validation, bounded attempts, database-clock deferral and fenced section checkpoint writes. Sixteen real PostgreSQL checks include three claim processes and a SIGKILL claimant with fixture-expired lease. Atomic staging is now wired to READY completion behind a default-off flag, but **not to a coordinator/graph-only processor**; complete automatic continuation remains open.
- [ ] Preserve validated checkpoints and resume only unfinished batches; retain lease fencing and deletion semantics.
- Both section and relationship-recovery checkpoints can now be saved under an independent graph-job fence. Recovery identity is rebuilt from validated saved section output; explicit empty results survive a PostgreSQL round trip and resume without model calls. This is a checkpoint contract, not a complete graph-only processor/publication path; keep the above item open.
- [x] Add estimated token reservations and optional caps alongside request limits; labelled as estimates, not actual provider usage or a hard quota guarantee.
- [ ] Account explicitly for hosted embedding and reranking work and its independent limits.
- [ ] Cache only validated extraction results with content, provider/model, prompt and schema versions; rebind provenance safely and invalidate stale entries.
- Per-upload checkpoints now have source/configuration/prompt/schema fingerprints, strict persisted-output validation and provenance rebinding. Twenty offline checks include serialization into a fresh process. This prerequisite was implemented before automatic graph-job processing because the old checkpoint identities could reuse incompatible model output. Cross-upload cache remains pending; keep the above acceptance item open.
- [ ] Test concurrent reservations, quota reset, failure/cancellation, restart/resume, priority and cache invalidation before deployment.

The initial characterization suite is `tests/test_provider_admission_contract.py`. It makes no database or provider requests. These tests protect existing boundaries; they do not demonstrate separate-process operation or the new priority/token/cache features.

Implementation order for the next run: (1) implement graph-only processing and an atomic, cancellation-safe, document/task/job-fenced Neo4j publication/completion path; add bounded dispatch and logical snapshot storage accounting/retention before activation. Section/recovery checkpoint saves and atomic READY/job staging are verified; keep `GRAPH_CONTINUATION_SNAPSHOTS_ENABLED=false` until remaining gates pass. (2) Verify real separate API/ingestion workers, full hard-kill recovery and cross-store consistency; (3) integrate hosted embedding/reranking admission and provider-reported token usage where available; (4) cache versioned validated graph batches across uploads while safely rebinding provenance. See [the activation gate](GRAPH_CONTINUATION_JOBS.md#next-implementation-gate). Do not rotate the original READY task or weaken existing document fences. Accepted manual retries still follow the full original pipeline, although completed graph batches are reused. Hosted Qdrant inference is executed inside vector operations and needs its own admission boundary; Cohere's comparison path makes two distinct requests. Do not count one user question as one universal provider request. Admission/holdback primitives are tested; do not mark Phase 3 complete or claim their zero-default enforcement is active in production.

More workers cannot create additional free provider capacity. Current entry points: `app/services/shared_provider_service.py`, `provider_metrics.py`, `document_processing_service.py`, `ingestion_service.py`, `app/core/processing_coordinator.py`, `app/services/upload_service.py`, `app/worker.py`, `scripts/start_local.py`.

## Phase 4 — Run a realistic scaling benchmark

- [ ] Varied corpus: short notes, long manuals, scans, tables and repeated terminology.
- [ ] Increasing concurrent uploads and queries with real separate workers.
- [ ] Record queue wait, ingestion throughput, query p50/p95, failures and tokens with workload/configuration hashes.
- [ ] Hard-kill worker recovery, not only graceful cancellation.
- [ ] Verify interrupted processing/deletion leaves no orphaned test data across stores.
- [ ] Separate real integration measurements from mock-backed contracts. Hosted load testing requires an explicit quota budget.

## Phase 5 — Optimize storage and search after profiling

- [ ] Verify relevant Qdrant payload indexes exist before ingestion.
- [ ] Profile/batch vector writes without weakening acknowledgement or cleanup guarantees.
- [ ] Profile PostgreSQL eligibility and pagination queries with representative data.
- [ ] Bound Neo4j traversal in database queries, not just the displayed graph.
- [ ] Record before/after measurements and correctness tests. Do not add sharding, Redis or another queue without evidence.

## Phase 6 — Improve document understanding

- [ ] Preserve text-block coordinates and reading order with page provenance.
- [ ] Test multi-column and table handling against labelled fixtures.
- [ ] Display the source image beside uncertain extraction.
- [ ] Provide protected reviewer corrections and rebuild affected chunks safely.
- [ ] Evaluate scans against human-checked reference text; do not promise handwriting, equations-to-LaTeX or diagram understanding without tests.

## Phase 7 — Document revision comparison

- [ ] Store explicit revision metadata without confusing distinct uploads.
- [ ] Match corresponding sections and flag ambiguous alignment.
- [ ] Show deterministic text differences and both source pages.
- [ ] Generate a grounded change summary only when provider use is authorized; keep the deterministic flow useful offline.
- [ ] Require human review for ambiguous changes; test missing sections, renamed headings, tables and conflicting versions.

## Phase 8 — Evaluate answers and relationships

- [ ] Human-reviewed answer correctness, claim-level citation support, completeness and unsupported refusals.
- [ ] Relationship type/direction support and OCR text quality against human references.
- [ ] Latency and token consumption with real-versus-estimated usage labelled.
- [ ] Repeat on prompt/model/chunking/retrieval changes with versioned datasets and independent holdouts.
- [ ] Label heuristic confidence as an evidence score unless calibrated.
- [ ] Leave absent reviews unmeasured and distinguish machine checks from human-reviewed results.

## Scheduled continuation

App heartbeat `conceptgraph-phases-3-8`: every four hours, returning to this chat. It resumes the current unfinished checkpoint, does not guarantee one phase per run, and reports meaningful results or actionable blockers. Disable after verified Phase 8 completion. Local execution requires the computer and desktop app to remain running.

## Release checkpoint

- Release preparation: 362 backend tests, nine frontend tests, TypeScript/Vite build and compile checks passed. Phase 1 report hashes match current runtime code; Phase 2 evaluator hashes match the saved offline report.
- Phase 1–2 release `9c8568cc66a076baf7d6cab05859115d3a3e2f7e` was pushed only to the portfolio repository. Both existing free Render services report Live. API readiness independently returns this exact revision with no degraded services, health returns 200, and unauthenticated protected course discovery returns 401. The public equipment preview renders its saved answer, citations and three-node/two-edge graph without AI calls. Render startup logs show successful startup and port binding; root 404 is expected because the API has no root route.
- After the three Phase 3 characterization tests: all 365 backend tests, compile checks and diff checks pass; frontend remains unchanged with nine tests and TypeScript/Vite build passing. GitHub Actions run `38272293936` passed for release `9c8568c` (backend and frontend jobs).
- Production semantic checks remain unmeasured; no provider quota was consumed by these release tests.
- Scheduled Phase 3 admission increment: ten real isolated PostgreSQL checks and all 378 backend tests pass; nine frontend tests and TypeScript/Vite build pass. Additive migration preserves old counts/SQL. No provider quota, production test data, environment changes or paid resources. Record the actual release/deployment result separately after verification.
- Scheduled retry-eligibility increment: ten PostgreSQL/ASGI checks, eleven focused regressions and all 389 backend tests pass. Nine frontend tests and TypeScript/Vite build pass. No migration, provider calls, environment changes or production test-data mutations. Automatic continuation remains pending. Record release/deployment verification separately.
- Scheduled checkpoint-compatibility increment: 20 focused offline checks, including a fresh subprocess, and all 409 backend tests pass. Nine frontend tests, TypeScript/Vite build and compile/diff checks pass. No migration, provider calls, environment changes or production test-data mutations. Existing unversioned checkpoint rows are retained but cannot be safely reused after a future requested rebuild. Automatic graph-only continuation and cross-upload caching remain pending. Record release/deployment verification separately.
- Scheduled graph-job foundation increment: sixteen isolated PostgreSQL checks, nine offline snapshot/due-time checks and all 418 backend tests pass; nine frontend tests, TypeScript/Vite build and compile/diff checks pass. Additive table/index/constraint initialization and old-document SQL/restart compatibility are tested. No provider calls, environment changes, production test-data mutations or paid resources. This is a metadata lease/snapshot/checkpoint service, not an activated scheduler or graph-only processor; full worker/cross-store acceptance remains open. Record actual release/deployment verification separately.
- Scheduled atomic staging increment: nine new PostgreSQL transaction checks, four offline wiring/configuration tests and all 422 backend tests pass; prior sixteen job-metadata and ten manual-retry database checks were rerun successfully. Nine frontend tests, build/compile/diff checks pass. No migration, hosted provider calls, environment/credential changes or production test-data mutations. Staging defaults off; no automatic production graph work is activated. Record actual release/deployment verification separately.
- Scheduled recovery-checkpoint increment: seven new offline identity tests and all 429 backend tests pass; nineteen isolated job-metadata/recovery database checks, twenty compatibility tests, nine atomic staging and ten manual-retry checks pass on current code. Nine frontend tests/build/compile/diff checks pass. No migration, hosted calls, environment edits or production test-data mutations. Snapshot staging remains default-off and no automatic processor/dispatcher/publication is enabled. Record actual deployment verification separately.
