# ConceptGraph implementation and verification ledger

Updated: 2026-10-16 (India). This is the current roadmap; older numbered scaling phases in the handbook describe earlier work.

## Operating boundaries

- User scope update, 14 October 2026: remove practice-exam generation and its generated-answer/retrieval service. Do not reintroduce it during scheduled phases. Main PDF Q&A, vector/graph retrieval and citations remain in scope and are preserved. Removal checks: 445 backend tests, nine frontend tests and the production build pass. Record release/deployment verification separately; historical reports describe their own prior revisions.
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
| 3 — Provider-safe ingestion | Partial: local extraction/deferral stage verified; defaults off | Authoritative input reads and independent lease/checkpoint handling are implemented locally. Latest checks: 462 backend tests and 23 isolated PostgreSQL job/stage checks pass, with zero hosted calls. Neo4j projection remains a separately verified primitive, not cross-store completion. Next: fenced publication/crash reconciliation, real stage restart tests, snapshot accounting and bounded dispatch. No automatic continuation/staging enabled or local stage deployed. Hosted admission/actual tokens, split-worker tests and cross-upload cache remain unfinished. |
| 4 — Realistic scaling | Pending | Validate separate-process behavior with varied local fixtures and hard-kill recovery; disclose all doubles. |
| 5 — Profile-guided storage/search | Pending | Measure first, then optimize actual filter/index/write/traversal bottlenecks. |
| 6 — Document understanding | Pending | Coordinates/order/tables, uncertainty review/corrections and human-reference OCR tests. |
| 7 — Revision comparison | Pending | Metadata, section alignment, deterministic differences, cited summaries and review. |
| 8 — Repeatable semantic evaluation | Pending human review | Extend Phase 2 foundation; do not confuse mechanical checks with reviewed correctness. |

## Phase 3 — Scale ingestion without overwhelming providers

15 October local checkpoint: added `GraphContinuationService.load_input`, which returns persisted snapshot chunks and validated checkpoints together under the existing claim fence, then releases read locks before extraction. This prevents the future processor from pairing trusted checkpoints with stale caller-supplied chunks. The existing checkpoint-only reader remains compatible. Nine focused tests pass; full offline suite: 446 tests pass. This helper is not dispatched, published or deployed; automatic continuation remains disabled and cross-store completion is still unfinished.

Follow-up offline verification: added rejection tests for independently changed snapshot digest and extraction contract, plus cancellation propagation/read-transaction release. `.venv/bin/python -m unittest discover -s tests -q`: 448 tests pass (4.004 seconds); `git diff --check` passes. No provider calls, database schema changes or production mutations. These are mocked transaction-boundary tests, not a real-store cancellation/hard-kill claim. The local input-reader group remains unreleased; the next implementation checkpoint is still graph-only processing and safe cross-store publication, not Phase 4.

Isolated PostgreSQL follow-up: updated the graph-job runner to resume extraction from `load_input`'s persisted chunks, even when the supplied claim has no chunks, rather than pairing checkpoint output with a separate caller list. All 21 checks pass, zero hosted calls, randomized test schema removed and temporary cluster stopped. Initial run correctly refused a non-allowlisted fixture-directory name; corrected runs encountered permission-review timeouts and sandbox socket denial before the escalated run passed. These checks remain metadata/checkpoint tests, not a complete graph worker or cross-store crash benchmark. Production continuation remains disabled; no release was made.

15 October extraction-stage checkpoint: added internal `app/services/graph_job_extraction.py`. It uses authoritative inputs, renews the independent job lease before extraction and periodically, saves batches through the existing fenced checkpoint service, cancels/drains extraction on lease loss and checks authority again before returning output. It never parses PDFs, writes embeddings, rotates the document task, publishes Neo4j output or marks a job completed. Six mocked orchestration tests cover resume/callbacks, initial/final fence loss, checkpoint rejection, cancellation and periodic lease loss. Full offline suite: 454 tests pass; diff whitespace check passes. No dispatcher imports this module, no hosted calls were made and no deployment was attempted. This is an extraction stage, **not** completed graph continuation: real-store stage/restart tests, durable quota deferral integration, fenced publication/crash reconciliation, dispatch/storage gates remain open.

Cancellation follow-up: a regression test first failed because a second caller cancellation could interrupt the extraction task's asynchronous cleanup. The stage now drains both extraction and lease tasks through the existing cancellation-shielded `run_async_safely` helper. Nine orchestration tests also cover invalid leases and propagation of extraction failures; the full offline suite passes 457 tests (4.346 seconds), and `git diff --check` passes. These remain mocked lifecycle tests, not proof of rollback after SIGKILL. All new stage/input changes remain local and automatic continuation is still disabled.

16 October local quota-deferral integration: the internal extraction stage now stops its renewal task before calling the existing fenced durable `defer` operation when output explicitly reports provider quota plus a validated retry delay. It raises `GraphJobDeferred` rather than returning that attempt for publication. Failed fence checks are not reported as scheduled; unknown resets do not invent deadlines or trigger an immediate retry. A started deferral transaction drains on cancellation. Five additional mocked tests cover these paths (14 stage tests total); full offline suite: 462 tests pass (1.710 seconds), and `git diff --check` passes. This connects existing durable scheduling metadata to the extraction stage only: there is still no dispatcher or completed publication path, and no new real-store/hard-kill result is claimed. No hosted calls, deployment or production changes.

16 October real-store stage verification: extended `scripts/evaluate_graph_continuation_jobs.py` to run the actual `GraphJobExtraction` stage against isolated PostgreSQL leases and checkpoints. Fully cached sections/recovery resume without provider calls; a mocked quota result persists its 90-second deadline, clears the old claim, prevents early claiming and preserves the READY document result. All 23 PostgreSQL checks pass; randomized schema removed and temporary server stopped. Full offline suite rerun: 462 tests pass (1.874 seconds); `git diff --check` passes. The report hashes now include the stage and its tests. This is not a real-provider extraction, API/worker benchmark or cross-store hard-kill test. Changes remain local; publication remains the next gate.

16 October stronger process-recovery check: replaced the claim-only killed child with the actual graph-only extraction stage. The child signals readiness only after loading two current checkpoints and renewing its real PostgreSQL lease, then pauses in a mocked extraction operation. Parent sends SIGKILL, explicitly expires the fixture lease, reclaims with a new token and runs the stage again; cached sections/recovery yield two concepts without provider calls. All 23 isolated PostgreSQL checks and 462 offline tests pass (3.154 seconds for the latter); diff whitespace check passes. The test schema was removed and temporary cluster stopped. This proves this stage's metadata/checkpoint recovery only—not new-batch persistence during a kill, natural lease timing, coordinator dispatch, Neo4j publication, Qdrant/R2 consistency or production capacity. Do not mark full split-worker/hard-kill acceptance complete. No deployment or automatic continuation activation.

16 October scoped release preparation: input-reader/extraction-stage code, tests and sanitized PostgreSQL report are ready for an incremental release. Nine frontend tests and `npm run build` (TypeScript plus Vite) pass; no frontend, environment, infrastructure or provider configuration was changed. Shipping these internal helpers does not enable automatic work: the coordinator still does not import the extraction stage and snapshot staging remains default-off. Record actual pushed/deployed revisions separately after verification; this entry does not assert deployment success or Phase 3 completion.

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

Implementation order for the next run: (1) implement graph-only processing and document/task/job-fenced publication/completion using the internal [transactional projection primitive](GRAPH_PROJECTION_REPLACEMENT.md). Shield the whole fenced Neo4j/PostgreSQL completion unit, reconcile ambiguous commits and test actual-store rollback/crash behavior; the primitive alone is not cross-store atomicity; add bounded dispatch and logical snapshot storage accounting/retention before activation. Section/recovery checkpoint saves and atomic READY/job staging are verified; keep `GRAPH_CONTINUATION_SNAPSHOTS_ENABLED=false` until remaining gates pass. (2) Verify real separate API/ingestion workers, full hard-kill recovery and cross-store consistency; (3) integrate hosted embedding/reranking admission and provider-reported token usage where available; (4) cache versioned validated graph batches across uploads while safely rebinding provenance. See [the activation gate](GRAPH_CONTINUATION_JOBS.md#next-implementation-gate). Do not rotate the original READY task or weaken existing document fences. Accepted manual retries still follow the full original pipeline, although completed graph batches are reused. Hosted Qdrant inference is executed inside vector operations and needs its own admission boundary; Cohere's comparison path makes two distinct requests. Do not count one user question as one universal provider request. Admission/holdback primitives are tested; do not mark Phase 3 complete or claim their zero-default enforcement is active in production.

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
- Scheduled projection-primitive increment: ten new offline boundary tests and all 439 backend tests pass; nine frontend tests/build/compile/diff checks pass. The existing query writer and cancellation guard are shared without changing ordinary ingestion parameters. No migration, environment edits, hosted calls or production test-data mutations. Earlier PostgreSQL reports were not rerun; actual Neo4j rollback/cross-store crash tests remain pending. The primitive is not wired to processing or dispatch; defaults remain off. Record actual deployment verification separately.
- Scheduled real projection verification: seven checks pass against isolated Neo4j 5.26.26. The installed image is reused without a pull; a dedicated loopback-bound container/network and authored data are removed afterward. Real rollback/replay/other-upload preservation, driver retry with an injected exception and cancellation draining are verified, not full cross-store completion or hard-kill recovery. Early network/log-allocation harness failures were corrected and disclosed. All 439 backend tests, nine frontend tests/build/compile/diff checks pass again. No application runtime change, migration, hosted AI calls, production test-data writes or environment edits. Automatic continuation remains disabled. Record actual release/deployment verification separately.
- Scheduled qualified-read increment: seven new offline tests and all 446 backend tests pass. Twenty-one isolated PostgreSQL graph-job checks cover persisted snapshot authority, stale read rejection, filtering, recovery invalidation and read-lock release; twenty compatibility, nine staging and ten retry checks pass again. Nine frontend tests/build/compile/diff checks pass. No migration, hosted calls, environment/credential changes or production test-data writes. Reader remains internal and disconnected from a graph-only processor; flags stay off. Projection code is unchanged. Record actual release/deployment verification separately.
- Qualified-read CI correction: the first CI run (38581548872) failed one new test because it assumed a configured Groq route from local settings. The fixture now explicitly configures synthetic settings; an added regression verifies unused-model stability with no provider key. Eight focused tests pass both in the project and from `/private/tmp` without its `.env`; all 447 backend tests pass. The 21/9/10 isolated PostgreSQL reports were refreshed. Runtime routing/reader code is unchanged by this correction. Verify the corrected release's CI/deployment before reporting success.
