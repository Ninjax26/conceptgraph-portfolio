# ConceptGraph implementation and verification ledger

Updated: 2026-10-12. This is the current roadmap; older numbered scaling phases in the handbook describe earlier work.

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
| 3 — Provider-safe ingestion | Started: audit and admission characterization | Shared PostgreSQL budgets currently count LLM attempts only. No token reservations, hosted embedding/rerank reservations or interactive capacity reserve. Existing checkpoint reload is per-upload; cross-upload versioned cache is not implemented. Three new offline tests preserve admission-before-execution, failover/repair metadata, cancellation and fail-closed budget storage. Next: implement interactive capacity reservation and token admission with isolated persistence/concurrency tests. |
| 4 — Realistic scaling | Pending | Validate separate-process behavior with varied local fixtures and hard-kill recovery; disclose all doubles. |
| 5 — Profile-guided storage/search | Pending | Measure first, then optimize actual filter/index/write/traversal bottlenecks. |
| 6 — Document understanding | Pending | Coordinates/order/tables, uncertainty review/corrections and human-reference OCR tests. |
| 7 — Revision comparison | Pending | Metadata, section alignment, deterministic differences, cited summaries and review. |
| 8 — Repeatable semantic evaluation | Pending human review | Extend Phase 2 foundation; do not confuse mechanical checks with reviewed correctness. |

## Phase 3 — Scale ingestion without overwhelming providers

- [ ] Run the API and workers as genuinely separate local processes; verify shared claiming and worker presence.
- [ ] Reserve provider capacity for interactive answers over background graph extraction, including failover and repair attempts.
- [ ] Durably defer quota-limited graph work until eligible; avoid hot retry loops and unlimited attempts.
- [ ] Preserve validated checkpoints and resume only unfinished batches; retain lease fencing and deletion semantics.
- [ ] Add conservative token-aware reservations alongside request limits; distinguish estimates from vendor-reported tokens.
- [ ] Account explicitly for hosted embedding and reranking work and its independent limits.
- [ ] Cache only validated extraction results with content, provider/model, prompt and schema versions; rebind provenance safely and invalidate stale entries.
- [ ] Test concurrent reservations, quota reset, failure/cancellation, restart/resume, priority and cache invalidation before deployment.

The initial characterization suite is `tests/test_provider_admission_contract.py`. It makes no database or provider requests. These tests protect existing boundaries; they do not demonstrate separate-process operation or the new priority/token/cache features.

Implementation order for the next run: (1) characterize PostgreSQL reservation windows and concurrent locking using an isolated local database; (2) add per-operation capacity reservation for interactive requests with conservative token limits; (3) persist retry eligibility without replacing lease expiry as the fencing mechanism; (4) cache versioned validated graph batches while safely rebinding upload provenance. Hosted Qdrant inference is executed inside vector operations and needs its own admission boundary; Cohere's comparison path makes two distinct requests. Do not count one user question as one universal provider request.

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
