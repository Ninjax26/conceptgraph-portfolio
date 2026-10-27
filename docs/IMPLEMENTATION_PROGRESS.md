# ConceptGraph implementation and verification ledger

Updated: 2026-10-27 (India). This is the current roadmap; older numbered scaling phases in the handbook describe earlier work.

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
| 3 — Provider-safe ingestion | Partial: fenced replay, bounded drafts, provider admission, publication intent, remote marker and receipt deployed but disconnected | Fenced replay deployed at `5034ae6`; bounded drafts at `f08bc48`; provider admission and Cohere telemetry at `0c497da`; distinct durable [publication intent](GRAPH_PUBLICATION_INTENTS.md) at `9cffa4a`; optional Neo4j marker/read primitive at `db97255`; disconnected observed-marker receipt at `81b8a4a`; matching marker cleanup at `0ab762a`; exact obsolete-marker cleanup primitive at `6c3c699`; bounded deleted-source discovery at `4d9a57e`. The worker calls none of these publication operations. A local Neo4j test reproduced a late old overwrite; a disconnected remote generation fence now rejects that replay. A disconnected tombstone and deleted-source helper exist. Source generation is now recorded in intents and compared in receipts locally, but no worker passes it to Neo4j. Live deletion integration, cross-store completion/crash/late-write reconciliation and bounded dispatch remain before activation. Cohere rerank and Qdrant Cloud document/query requests reserve estimated budget; Cohere rerank reports search units. Qdrant-hosted usage is still unknown. Full provider-cost attribution, split-worker tests and cross-upload cache remain unfinished. |
| 4 — Realistic scaling | Pending | Validate separate-process behavior with varied local fixtures and hard-kill recovery; disclose all doubles. |
| 5 — Profile-guided storage/search | Pending | Measure first, then optimize actual filter/index/write/traversal bottlenecks. |
| 6 — Document understanding | Pending | Coordinates/order/tables, uncertainty review/corrections and human-reference OCR tests. |
| 7 — Revision comparison | Pending | Metadata, section alignment, deterministic differences, cited summaries and review. |
| 8 — Repeatable semantic evaluation | Pending human review | Extend Phase 2 foundation; do not confuse mechanical checks with reviewed correctness. |

## Phase 3 — Scale ingestion without overwhelming providers

27 October local source-generation increment: the upload's positive attempt
count is stored with a new publication intent. Receipt observation requires
the committed intent, current READY source and remote marker to agree on that
generation, then stores it in the receipt. Older rows retain a null generation
for deletion reconciliation and fail closed for publication replay. The
columns are additive and nullable; the isolated PostgreSQL runner rebuilt an
old table shape with retained intent/receipt rows, ran startup migration twice,
and confirmed the rows survived with unknown generation. Its 43 checks passed
with a private cluster removed and zero hosted-provider calls. The marked
Neo4j publisher remains disconnected, as do job completion and cross-store
reconciliation. The final backend suite passed 539 tests and 135 subtests;
nine frontend tests, TypeScript/Vite build, compileall and diff checks passed.
The first build and compile attempts failed because the sandbox denied Vite's
temporary config file and Python bytecode writes; the same checks passed with
workspace write permission. Record release verification separately.

27 October deletion-fence release `0f8731367f5a4b492bf867cd837329ef784bc799`:
11 scoped files were pushed to `origin/main`. [GitHub CI run 40469649852](https://github.com/Ninjax26/conceptgraph-portfolio/actions/runs/40469649852)
passed. The free Render API `/api/v1/ready` returned HTTP 200 with this exact
revision, combined processing role, zero queued jobs, no degraded services,
and graph/ingestion available. The public frontend `/sample` route returned
HTTP 200; the public sample API returned one prepared document with
`GRAPH_READY`. These checks do not exercise the disconnected tombstone or
graph publication path. The first readiness check timed out during a cold
start, and the next served the previous revision before the new one appeared.
Final local verification: 19 isolated Neo4j checks, 42 isolated PostgreSQL
checks, 536 backend tests, 133 subtests, nine frontend tests, TypeScript/Vite
build, compileall and diff checks passed. No hosted provider call or
production test-data mutation was made. Automatic continuation remains
disabled. This release-verification note is local until the next substantive
release.

27 October local deletion-fence increment: a disconnected Neo4j managed
transaction can now bind a publication fence to its course, permanently
tombstone a committed-deleted upload, and remove its concepts and publication
markers atomically. The marked writer rejects subsequent attempts, including
the first publication after deletion. Real isolated Neo4j checks cover
idempotence, wrong-course rejection and a tombstone racing an in-flight marked
write. A separate PostgreSQL helper checks retained intents against a fresh,
committed source view under `FOR UPDATE` before requesting a tombstone; a
rolled-back deletion authorizes no cleanup, and a remote failure is retryable.
The 19 isolated Neo4j checks and 42 isolated PostgreSQL checks passed with
temporary resources removed and zero hosted-provider calls. These are separate
tests: PostgreSQL's remote call is mocked. Neither helper has a runtime caller.
The backend suite passed 536 tests and 133 subtests; nine frontend tests,
TypeScript/Vite build, compileall and diff checks passed.
The live deletion route retains its existing cleanup order because an earlier
permanent tombstone could block a document if later R2 or PostgreSQL deletion
fails. Source generation in PostgreSQL, combined cross-store crash recovery,
completion and bounded dispatch remain before automatic continuation can run.
Record release verification separately.

27 October generation-fence release `03e9cc5767a1caf7e205fea767cf7887af3f9325`:
ten scoped files were pushed to `origin/main`. [GitHub CI run 40404315938](https://github.com/Ninjax26/conceptgraph-portfolio/actions/runs/40404315938)
completed successfully. The free Render API readiness endpoint returned HTTP
200 with this exact revision, combined processing role, zero queued jobs, no
degraded services, and graph/ingestion available. The public `/sample` route
rendered its saved answer, three source-page controls and a three-concept,
two-relationship graph. That prepared preview does not test the disconnected
marked publication path. The first readiness request timed out during a cold
start; the next succeeded. Final local verification: 15 isolated Neo4j checks,
39 isolated PostgreSQL checks, 533 backend tests, 133 subtests, nine frontend
tests, TypeScript/Vite build, compileall and staged diff checks passed. No
hosted provider call or production test-data mutation was made. Automatic
continuation remains disabled. This release-verification note is local until
the next substantive release.

27 October local late-write safety increment: the disconnected Neo4j
publication primitive first accepted an exact old marker replay after a newer
READY task had written its graph. A failing real-Neo4j regression captured
that overwrite. The marked replacement now requires an explicit positive
source generation and checks a unique, transaction-locked remote fence before
deleting concepts. The same regression then passed: older attempts and
different tasks or payloads at an equal generation cannot replace the newer graph. This
does not activate the path: deletion lacks a durable tombstone, the
PostgreSQL intent/receipt does not carry generation, and cross-store
reconciliation/worker wiring remain. The disposable Neo4j 5.26.26 run passed
15 checks; its container/network were removed and Docker Desktop was stopped.
The isolated PostgreSQL continuation runner passed 39 checks with its private
cluster removed. The backend suite passed 533 tests and 133 subtests;
compileall and `git diff --check` passed. No hosted provider or production data
was used. The first PostgreSQL command lacked its required socket argument;
the sandbox then blocked shared memory, and two private-cluster setup attempts
used the wrong socket path/role. The corrected isolated command passed.
Record release/deployment separately.

26 October deleted-source discovery release
`4d9a57eb4cf1c8bc36d62741bbb9acf5d688e43b`: seven scoped files
were pushed to `origin/main`. [GitHub CI run 40362701711](https://github.com/Ninjax26/conceptgraph-portfolio/actions/runs/40362701711)
passed. The public API `/api/v1/ready` returned HTTP 200 at this exact
revision, combined role, queue depth zero, no degraded services, with graph
and ingestion available. The public `/sample` page rendered its saved answer,
three source-page controls and a three-concept/two-relationship illustration.
This prepared preview does not test live graph writes or reconciliation.
All 532 backend tests, nine frontend tests, TypeScript/Vite build and 39
isolated PostgreSQL checks passed before pushing. The first build attempt
could not create Vite's temporary config file inside the sandbox; the same
build passed with repository write permission. No hosted model or production
test-data call was made. The new discovery method has no runtime caller;
Phase 3 remains incomplete. This release-verification note stays local until
the next substantive release.

26 October local deleted-source discovery checkpoint: a bounded, read-only
PostgreSQL query now lists retained publication intents whose upload row is
gone. One mocked contract test passes; the isolated PostgreSQL runner passes
39 checks, including this discovery after an actual fixture deletion while a
live-upload intent remains excluded. It neither reads Neo4j nor cleans up
anything, and a late writer can still appear afterward. The private local
cluster and test schema were removed. No hosted provider or production data
was used. Automatic continuation and reconciliation remain unfinished.

26 October obsolete-publication cleanup release
`6c3c699911da43a156393ddd818d63ef6363ac1d`: seven scoped files were
pushed to `origin/main`. [GitHub CI run 40325225703](https://github.com/Ninjax26/conceptgraph-portfolio/actions/runs/40325225703)
passed. The first public readiness call timed out during a cold start; the next
returned the prior revision, then `/api/v1/ready` returned HTTP 200 with this
exact revision, combined role, queue depth zero, no degraded services and
`graph_available=true`. The public `/sample` page rendered a saved answer,
three source-page links and a three-concept/two-relationship illustration.
That prepared preview does not test live graph writes or cleanup. All 531
backend tests, nine frontend tests, TypeScript/Vite build and 12 isolated
Neo4j checks passed before pushing. No hosted model or production test-data
call was made. The new cleanup method has no runtime caller; Phase 3 remains
incomplete. This release-verification note stays local until the next
substantive release.

26 October local obsolete-publication cleanup checkpoint: added a disconnected
Neo4j managed-transaction primitive to remove one exact marker and only concept
nodes carrying its original READY task token. A future caller must first prove
in PostgreSQL that the intent is obsolete; this method has no API, worker or
scheduler caller. Authored tests cover exact scope, missing/conflicting markers,
write failure and invalid identity. Full offline backend suite: 531 passed;
frontend: nine passed; TypeScript/Vite build and compile/diff checks passed.
The first isolated Neo4j attempt could not run because Docker Desktop's daemon
did not become ready. A later retry passed all 12 checks against a disposable
Neo4j 5.26.26 container, including obsolete and superseded projection cases;
its container and network were removed. No hosted provider or production data
was used. This still lacks PostgreSQL authority, late-write discovery,
hard-kill recovery and automatic continuation. Record release verification
separately.

26 October marker-cleanup release `0ab762aa41acc7b5184d3e0aa7f57c1fa6cb383f`:
seven scoped files were pushed to `origin/main`. [GitHub CI run 40292277384](https://github.com/Ninjax26/conceptgraph-portfolio/actions/runs/40292277384)
passed. The public API `/api/v1/ready` returned HTTP 200 with this exact
revision, combined processing role, queue depth zero, no degraded services and
`graph_available=true`. The public `/sample` page rendered a saved answer,
three source links and a three-concept/two-relationship illustration. That
prepared preview does not verify live graph writes or deletion. The isolated
Neo4j deletion test and all offline checks passed before the push. No hosted
provider or production test-data mutation was made. Late-write reconciliation
and automatic continuation remain unimplemented. This release-verification
note stays local until the next substantive release.

26 October local deletion-cleanup checkpoint: normal graph cleanup now removes
`GraphPublication` markers for the same upload and course alongside its concept
projection. It leaves neighboring uploads and courses untouched and is safe to
repeat. This closes an orphan-marker path for a deletion occurring after a
remote marker was written. The full 528-test backend suite, nine frontend
tests, TypeScript/Vite build and compile/diff checks pass. A disposable Neo4j
5.26.26 run passed 10 checks, including real scoped marker deletion; the
container and network were removed. Initial Neo4j verification could not
connect because Docker Desktop was stopped; after starting it, the rerun
passed. No hosted providers or production data were touched. A remote write
that arrives **after** cleanup, and PostgreSQL/Neo4j commit reconciliation,
remain untested and unimplemented; automatic continuation remains disabled.
Record release verification separately.

26 October receipt release `81b8a4aef6efe19abff096f104c7801740eec213`:
eight scoped files were pushed to `origin/main` after 528 backend tests, nine
frontend tests, the TypeScript/Vite build and 38 isolated PostgreSQL checks
passed. [GitHub CI run 40236470973](https://github.com/Ninjax26/conceptgraph-portfolio/actions/runs/40236470973)
passed. The public API `/api/v1/ready` returned HTTP 200 with that exact
revision, combined processing role, queue depth zero, no degraded services,
and `graph_available=true`. The public `/sample` page rendered a saved answer,
three source-page links and a three-concept/two-relationship illustration.
That editorial preview does not verify live graph writes. No hosted provider
or production test-data call was made. The receipt has no runtime caller;
automatic continuation remains disabled and Phase 3 is incomplete. This
release-verification note stays local until the next substantive release.

26 October local receipt checkpoint: added an additive
`graph_publication_receipts` table and internal method that observes an exact
Neo4j marker under the current document/job/source fence, checks the claim
again after remote I/O, and stages one immutable receipt without committing.
Rows have no cascading source foreign key, so they survive deletion for future
reconciliation. Five new offline tests bring the backend suite to 528 passing;
the isolated PostgreSQL runner passes 38 checks, including receipt replay,
schema reinitialization and deletion survival. Its marker reader is mocked;
the prior real Neo4j marker check remains separate. Nine frontend tests,
TypeScript/Vite build, compile and diff checks pass. The first PostgreSQL
harness attempt hung because `pg_ctl` inherited captured output pipes; its
exact disposable cluster was stopped and removed. A corrected run passed with
zero hosted calls and zero production mutations. There is still no publisher,
job completion, cross-store crash/deletion reconciler or activation. Record
release verification separately after pushing; this checkpoint alone does not
complete Phase 3.

25 October remote-marker release `db97255b14d6b89bbd44059f18a140cc1ef0e35e`: eight scoped files were pushed only to `origin/main`. [GitHub CI run 40217107770](https://github.com/Ninjax26/conceptgraph-portfolio/actions/runs/40217107770) passed. The public API `/api/v1/ready` returned HTTP 200 with this exact revision, combined processing role, queue depth zero and ingestion available. It still reported `degraded_services=[neo4j]` and `graph_available=false`, as the previous revision did; the cause remains unconfirmed. The public `/sample` page rendered its saved answer, three source-page links and a three-concept/two-relationship illustration. That prepared preview does not verify live Neo4j, and no frontend code changed. No hosted provider or production test-data call was made. The marker/intent have no runtime caller and automatic continuation remains disabled. This release-verification note remains local until the next substantive release to avoid a docs-only redeployment.

25 October local remote-marker checkpoint: an optional `GraphPublication` node is
written in the same Neo4j managed transaction as a replacement projection, with
exact intent/job/upload/course/READY-task/digest identity. A reader retrieves it
by intent ID, and a conflicting existing identity rolls back the replacement.
Existing production write calls omit the marker; there is no runtime caller for
publication intents or markers. The full offline suite passed 523 backend tests,
nine frontend tests, TypeScript/Vite build, compile and diff checks. The isolated
Neo4j 5.26.26 run passed nine checks, with its own container/network removed;
no hosted provider calls or production test-data mutations. This is **not** a
PostgreSQL receipt, completion protocol, deletion reconciler, or Phase 3 completion.
The refreshed report is [graph-projection-current.md](../evaluation/graph-projection-current.md).

25 October publication-intent release `9cffa4a2f73dda616e6df5e5c7fd7996a6bc8b35`: the eight scoped files were pushed only to `origin/main`. [GitHub CI run 40197802932](https://github.com/Ninjax26/conceptgraph-portfolio/actions/runs/40197802932) passed. The public API `/api/v1/ready` returned HTTP 200 with this exact revision, combined processing role, queue depth zero and ingestion available. It still reported `degraded_services=[neo4j]` and `graph_available=false`; the earlier revision had the same degradation, and its cause remains unconfirmed. The public `/sample` page rendered a saved answer, three source links and a three-concept/two-relationship illustration; that read-only preview does not test live Neo4j. No frontend code changed, and the static site's exact deployed commit was not exposed. No hosted model or production test-data call was made. Automatic graph continuation is still disabled; the intent has no runtime caller or remote-write receipt. This release-verification note remains local until the next substantive release to avoid a docs-only redeployment.

25 October local publication-intent checkpoint: added an additive `graph_publication_intents` table and internal `stage_publication_intent` operation. It records the exact validated draft digest under the current document/job fence, leaves commit to the caller and survives document/job deletion for future late-write reconciliation. It is disconnected from all runtime dispatch and performs no remote write. Four new offline tests bring the backend suite to 518 passing tests (`.venv/bin/python -m unittest discover -s tests -q`, 6.993 seconds). The isolated PostgreSQL graph-job runner passes 37 checks, including intent persistence across initialization/replay and survival after committed fixture deletion; it removes its random schema and makes zero hosted calls. Nine frontend tests, `npm run build` (TypeScript and Vite), `compileall` and diff checks pass. The first build and compile attempts failed because the default sandbox denied writing Vite's temporary config and Python bytecode under the repository; the approved build and a retry with `PYTHONPYCACHEPREFIX=/private/tmp/conceptgraph-pycache` passed. This is **not** a publication receipt, reconciler, safe late-write cleanup or Phase 3 completion. The existing staging/dispatch flags remain off. See [the intent contract](GRAPH_PUBLICATION_INTENTS.md) and the isolated report for source hashes. The release result is recorded above.

25 October provider-admission release `0c497da49fafc8c1ab1587951bb6709243441ecc`: pushed only to `origin/main`; [GitHub CI run 40160715804](https://github.com/Ninjax26/conceptgraph-portfolio/actions/runs/40160715804) completed successfully. The pre-push offline checks were 514 backend tests, nine frontend tests, TypeScript/Vite build, `compileall` and diff checks; see the local checkpoint below for timings and the unsupported generic `npm test` command. The public API `/api/v1/ready` returned HTTP 200 with this exact revision in combined mode, queue depth zero and ingestion available. It also reported `degraded_services=[neo4j]` and `graph_available=false`, as it had for the previous revision; the cause remains unconfirmed. The public `/sample` page rendered a saved answer, three source-page links and an illustrated three-concept/two-relationship graph, without a hosted model request. The Render dashboard was signed out, so service logs and the static frontend's exact deployed commit were not verified; no frontend code changed in this release. This release does not complete Phase 3 or establish live graph health. This verification note remains local until the next substantive release to avoid a duplicate docs-only deployment.

25 October local Cohere rerank-usage checkpoint: the Cohere v2 adapter now captures the response's reported `meta.billed_units.search_units` for each actual rerank HTTP call. These units are separate from estimated/reported tokens and are not presented as money or a universal quota count. The provider status metrics expose reported unit counts per process, provider and operation; responses without valid unit metadata remain unknown. The call measurement covers HTTP status/JSON receipt, records no query/document text and does not alter ranking behavior. Three new offline tests cover aggregation, invalid/missing metadata and a mocked Cohere response. Full backend suite: 514 tests pass (10.058 seconds); frontend: nine tests pass with `npm run test:frontend`; `npm run build` passes (TypeScript plus Vite, 14.23 seconds); `compileall` and `git diff --check` pass. No hosted calls or production mutations. An initial generic `npm test -- --run` failed because this repository has no `test` script; `npm run test:frontend` is the declared command and passed. Changes remain local and uncommitted. Qdrant-hosted embedding usage is not captured, and Cohere billed search units do not establish complete cost/quota accounting. Source: [Cohere v2 rerank response reference](https://docs.cohere.com/v2/reference/rerank).

20 October local provider-budget checkpoint: hosted Cohere reranking now calls the shared PostgreSQL reservation service before each HTTP request, counting the query plus bounded passages as estimated input tokens. Comparison facet reranking reserves separately per actual request. A denied reservation prevents the network call. Two new offline tests prove ordering, estimates and denial behavior using `httpx.MockTransport`; no hosted request was made. All 507 backend tests pass in 6.488 seconds; the focused facet suite passes 12 tests, and compile/diff checks pass. The live readiness check could not resolve `conceptgraph-api.onrender.com` from this environment, so no current deployment status is inferred. This does not add Cohere response-token telemetry, Qdrant-hosted inference admission or hard vendor billing/quota accounting. Runtime changes remain local and uncommitted in this checkpoint.

20 October local embedding-admission checkpoint: the document processor now reserves one shared `qdrant_cloud` request and a character-based token estimate before the batched Qdrant Cloud upsert. If budget admission denies the operation, vector indexing is not started and existing failure/cleanup handling applies. Two offline tests verify reserve-before-upsert and denial behavior; the mocks issue no Qdrant or provider calls. Combined with the Cohere tests, the backend suite now contains 509 passing tests. This accounts for hosted document indexing only: query-time Qdrant inference and retries internal to the SDK remain unmeasured. The reservation is conservative and estimates text/4; it is not vendor billing data. Changes remain local, uncommitted and undeployed.

20 October local retrieval-admission checkpoint: Qdrant Cloud query embeddings now reserve one interactive provider request using the exact expanded query text before dense vector retrieval. A denied reservation prevents vector search. Two offline regressions check ordering, the token estimate and denial behavior; no Qdrant/network requests occur in these tests. The full backend suite has 511 passing tests (4.837 seconds); nine frontend tests pass, and `compileall` plus `git diff --check` pass. The production build command was started, but its completion was not captured, so it is not claimed as passing. Reservations remain estimates, not Qdrant billing telemetry; internal Qdrant retries and requests skipped because a collection is absent can make reserved counts differ from vendor inference usage. This closes application-side pre-admission for the Qdrant Cloud document and query paths plus Cohere reranking, but not provider-reported usage. Changes are local, uncommitted and undeployed. The live readiness curl and `git ls-remote origin HEAD` both failed at DNS resolution (`conceptgraph-api.onrender.com` and `github.com` respectively); no CI, push, or current deployment status can be claimed.

19 October bounded-draft release `f08bc48f894cb127b62b78fb89a7b16f9e26faf3`: scoped changes pushed only to origin; GitHub CI `39283457598` passes. Fresh local release checks: 505 backend tests (3.882 seconds), 35 isolated PostgreSQL checks, nine frontend tests, TypeScript/Vite build (5.86 seconds), compile/diff checks and read-only review pass. Fixture schema/server/directory removed. Public API serves the exact SHA in combined mode, with PostgreSQL/Qdrant/object storage and ingestion readiness passing, but two probes report `degraded_services=[neo4j]`, `graph_available=false`. Do not describe this as fully healthy deployment. Public saved sample renders its answer, three source-page links and three-node/two-edge illustrated graph; this offline preview does not prove live Neo4j health. Browser startup initially timed out but recovered. Render dashboard redirects to sign-in, preventing startup/error-log diagnosis and exact frontend revision inspection. No connection/readiness code changed in this release; cause of Neo4j degradation is unconfirmed. Need Render sign-in/log access before proposing a fix, not speculative credential changes or repeated deployments. Automatic continuation remains disabled; late-write-safe intent/receipt and cross-store crash reconciliation remain unfinished. This entry is local follow-up evidence, not another release.

19 October concurrency verification resumed: fixture startup approval succeeded. All 35 isolated PostgreSQL checks pass with zero hosted calls: two concurrent sessions competing for the last permitted draft slot admit exactly one, retain both READY results, and clean up only their authored fixture documents; terminal cleanup skips a document locked by a second session. All 505 offline backend tests pass again (4.929 seconds from `/private/tmp` without project `.env`); compile/diff checks pass. Random schema removed and server stopped. This supersedes the preceding approval blocker, not its historical account. Two sessions in one process are not independent ingestion workers, multi-store consistency, crash recovery or measured capacity. Runtime code is unchanged in this verification increment. Draft changes remain local/uncommitted; late-write-safe remote intent/receipt, ambiguous-commit/deletion reconciliation and bounded dispatch remain the next gates. No automatic activation or production mutation.

19 October concurrency-check preparation (not executed against PostgreSQL): extended the isolated runner with two concurrent session saves contending for one remaining draft slot, READY-result preservation and fixture-only cleanup; added a second-session document lock to check terminal cleanup skips busy rows. No runtime behavior changed. Starting the new private socket fixture was not executed: automatic permission review timed out, including its one permitted retry. The prior 33-check report remains historical evidence for its recorded hashes; do not claim 35 checks or current concurrency verification. The old stopped fixture directory `/private/tmp/conceptgraph-budget-pg.dYAWJZ` remains; `/private/tmp/conceptgraph-budget-pg.vGlqlb` was allocated but never initialized or started. Neither has a postmaster PID. No database/network/provider calls or production mutations were made by the new tests. Resume only after local process/socket execution approval is available; do not bypass approval or enable continuation. Draft schema and these test changes remain local/uncommitted.

19 October local draft-budget/retention checkpoint: optional default-zero `GRAPH_DRAFT_MAX_JOBS` / `GRAPH_DRAFT_MAX_BYTES` now serialize new admissions under the installation lock, count all retained drafts and enforce logical UTF-8 text/count budgets independently of snapshot limits. Identical saved drafts remain available at the cap; denied saves stage nothing. Unscheduled `prune_terminal_drafts` bounds work, skips locked documents, rechecks old terminal jobs and deletes only draft rows under document/job/draft locks; caller owns commit/rollback. Repeated same-transaction cleanup flushes pending deletions. Four additional offline tests and all 505 backend tests pass (4.699 seconds from `/private/tmp` without project `.env`); 33 isolated PostgreSQL checks pass with zero hosted calls. Initial DB test incorrectly expected pruning immediately after snapshot maintenance refreshed `updated_at`; the corrected fixture explicitly tests recent exclusion, then ages only fixture metadata. Retention guard was not weakened. Schema removed and temporary server stopped. Compile/diff checks pass. Draft schema/storage/retention remain local, uncommitted and undispatched; concurrent draft admission/busy-cleanup tests, late-write-safe intent/receipt, cross-store crash reconciliation and dispatch remain open. No production mutation or deployment.

19 October local draft-persistence checkpoint: added the additive `GraphPublicationDraft` table and internal save/load operations under current document/job/source fences. One exact candidate per job is immutable through these operations; identical saves are idempotent even within an autoflush-disabled transaction. Successful calls retain locks and leave commit/rollback to the caller. Body bytes are limited to 8 MiB in code and PostgreSQL; loading revalidates saved content against current sources. Six new mocked tests and all 501 backend tests pass (2.801 seconds, from `/private/tmp` without project `.env`); 31 isolated PostgreSQL checks pass, including rollback/reinitialization/new-session preservation, conflicting draft rejection, SQL body constraint and cascade rollback preserving READY output. Test schema removed; disposable server stopped. Zero hosted calls, production mutations or activation. See [draft contract and rollback](GRAPH_PUBLICATION_DRAFTS.md). This schema/code remains local and uncommitted: installation-wide draft storage accounting/retention, a separate late-write-safe publication intent/receipt and actual-store crash/deletion reconciliation remain unfinished. It is not safe to repurpose the cascading draft as a remote-commit receipt. Previous release `5034ae6` remains the deployed baseline.

18 October fenced replay checkpoint: `GraphContinuationService.restore_publication` now reloads the current document/job claim, persisted snapshot and extraction contract before restoring a candidate. Preparation and restoration share the same internal source validation; neither commits/releases successful locks nor performs network I/O. Three new mocked boundary tests and all 495 backend tests pass (2.947 seconds, run from `/private/tmp` without project `.env`). The isolated PostgreSQL runner passes 28 checks, including current-source replay with caller chunks ignored, a competing row-lock attempt blocked until rollback, and stale-token rejection. It removes its random schema; the disposable server was stopped and removed. Zero hosted calls or production mutations. The report hashes reflect this increment. No durable publication intent/receipt, remote graph write, job completion or automatic dispatch has been implemented; cross-store crash reconciliation remains the next gate. Record any actual release separately.

18 October replay-validation checkpoint: added internal `restore_graph_publication` beside publication preflight. It verifies the exact saved JSON digest, revalidates graph structure, rebuilds the candidate from caller-supplied current source execution and requires exact canonical equality. Corruption, malformed payloads, changed evidence, foreign identity, coerced page types and unexpected envelope fields are rejected, even with a recomputed checksum. Four additional tests cover round-trip independence and rejection paths; all 14 candidate tests and 492 backend tests pass (full suite 3.393 seconds from `/private/tmp`, without project `.env`), compile and diff checks pass. No database schema, provider call, production mutation or dispatcher change. This helper is local/unreleased and has no durable storage or runtime caller. A checksum is not authentication, semantic truth or a live lease: the future publisher must load current authority under locks and implement durable intent/receipt plus ambiguous-commit/deletion reconciliation. The previous PostgreSQL report was not regenerated for this pure helper.

15 October local checkpoint: added `GraphContinuationService.load_input`, which returns persisted snapshot chunks and validated checkpoints together under the existing claim fence, then releases read locks before extraction. This prevents the future processor from pairing trusted checkpoints with stale caller-supplied chunks. The existing checkpoint-only reader remains compatible. Nine focused tests pass; full offline suite: 446 tests pass. This helper is not dispatched, published or deployed; automatic continuation remains disabled and cross-store completion is still unfinished.

Follow-up offline verification: added rejection tests for independently changed snapshot digest and extraction contract, plus cancellation propagation/read-transaction release. `.venv/bin/python -m unittest discover -s tests -q`: 448 tests pass (4.004 seconds); `git diff --check` passes. No provider calls, database schema changes or production mutations. These are mocked transaction-boundary tests, not a real-store cancellation/hard-kill claim. The local input-reader group remains unreleased; the next implementation checkpoint is still graph-only processing and safe cross-store publication, not Phase 4.

Isolated PostgreSQL follow-up: updated the graph-job runner to resume extraction from `load_input`'s persisted chunks, even when the supplied claim has no chunks, rather than pairing checkpoint output with a separate caller list. All 21 checks pass, zero hosted calls, randomized test schema removed and temporary cluster stopped. Initial run correctly refused a non-allowlisted fixture-directory name; corrected runs encountered permission-review timeouts and sandbox socket denial before the escalated run passed. These checks remain metadata/checkpoint tests, not a complete graph worker or cross-store crash benchmark. Production continuation remains disabled; no release was made.

15 October extraction-stage checkpoint: added internal `app/services/graph_job_extraction.py`. It uses authoritative inputs, renews the independent job lease before extraction and periodically, saves batches through the existing fenced checkpoint service, cancels/drains extraction on lease loss and checks authority again before returning output. It never parses PDFs, writes embeddings, rotates the document task, publishes Neo4j output or marks a job completed. Six mocked orchestration tests cover resume/callbacks, initial/final fence loss, checkpoint rejection, cancellation and periodic lease loss. Full offline suite: 454 tests pass; diff whitespace check passes. No dispatcher imports this module, no hosted calls were made and no deployment was attempted. This is an extraction stage, **not** completed graph continuation: real-store stage/restart tests, durable quota deferral integration, fenced publication/crash reconciliation, dispatch/storage gates remain open.

Cancellation follow-up: a regression test first failed because a second caller cancellation could interrupt the extraction task's asynchronous cleanup. The stage now drains both extraction and lease tasks through the existing cancellation-shielded `run_async_safely` helper. Nine orchestration tests also cover invalid leases and propagation of extraction failures; the full offline suite passes 457 tests (4.346 seconds), and `git diff --check` passes. These remain mocked lifecycle tests, not proof of rollback after SIGKILL. All new stage/input changes remain local and automatic continuation is still disabled.

16 October local quota-deferral integration: the internal extraction stage now stops its renewal task before calling the existing fenced durable `defer` operation when output explicitly reports provider quota plus a validated retry delay. It raises `GraphJobDeferred` rather than returning that attempt for publication. Failed fence checks are not reported as scheduled; unknown resets do not invent deadlines or trigger an immediate retry. A started deferral transaction drains on cancellation. Five additional mocked tests cover these paths (14 stage tests total); full offline suite: 462 tests pass (1.710 seconds), and `git diff --check` passes. This connects existing durable scheduling metadata to the extraction stage only: there is still no dispatcher or completed publication path, and no new real-store/hard-kill result is claimed. No hosted calls, deployment or production changes.

16 October real-store stage verification: extended `scripts/evaluate_graph_continuation_jobs.py` to run the actual `GraphJobExtraction` stage against isolated PostgreSQL leases and checkpoints. Fully cached sections/recovery resume without provider calls; a mocked quota result persists its 90-second deadline, clears the old claim, prevents early claiming and preserves the READY document result. All 23 PostgreSQL checks pass; randomized schema removed and temporary server stopped. Full offline suite rerun: 462 tests pass (1.874 seconds); `git diff --check` passes. The report hashes now include the stage and its tests. This is not a real-provider extraction, API/worker benchmark or cross-store hard-kill test. Changes remain local; publication remains the next gate.

16 October stronger process-recovery check: replaced the claim-only killed child with the actual graph-only extraction stage. The child signals readiness only after loading two current checkpoints and renewing its real PostgreSQL lease, then pauses in a mocked extraction operation. Parent sends SIGKILL, explicitly expires the fixture lease, reclaims with a new token and runs the stage again; cached sections/recovery yield two concepts without provider calls. All 23 isolated PostgreSQL checks and 462 offline tests pass (3.154 seconds for the latter); diff whitespace check passes. The test schema was removed and temporary cluster stopped. This proves this stage's metadata/checkpoint recovery only—not new-batch persistence during a kill, natural lease timing, coordinator dispatch, Neo4j publication, Qdrant/R2 consistency or production capacity. Do not mark full split-worker/hard-kill acceptance complete. No deployment or automatic continuation activation.

16 October scoped release preparation: input-reader/extraction-stage code, tests and sanitized PostgreSQL report are ready for an incremental release. Nine frontend tests and `npm run build` (TypeScript plus Vite) pass; no frontend, environment, infrastructure or provider configuration was changed. Shipping these internal helpers does not enable automatic work: the coordinator still does not import the extraction stage and snapshot staging remains default-off. Record actual pushed/deployed revisions separately after verification; this entry does not assert deployment success or Phase 3 completion.

Release follow-up: internal extraction checkpoint `0d7124a7690471166b428dd1aa9ea09347e17d60` is on origin and both existing free Render services; CI `38930636675` passed. Exact API readiness revision and public saved preview were verified; browser health succeeded after shell timeouts. Automatic continuation remains disabled. This supersedes earlier pre-release "local/not deployed" wording, not the outstanding acceptance gates.

17 October local snapshot-capacity checkpoint: added optional `GRAPH_SNAPSHOT_MAX_JOBS` and `GRAPH_SNAPSHOT_MAX_BYTES`, both zero/default-disabled. New or replacement snapshot admission takes a transaction-scoped installation lock, flushes same-transaction staging without committing, and checks all retained job statuses against count and PostgreSQL JSONB-text byte limits. Replaced upload is excluded from old usage; duplicate same-execution scheduling remains idempotent; no eviction or production data deletion occurs. Two offline policy tests and real byte-boundary/concurrent scheduling checks pass: 464 backend tests and 25 isolated PostgreSQL checks, zero hosted calls. Two competing schedules with one remaining slot admitted exactly one. Bytes are logical JSON text, not physical database/WAL/storage capacity. The test schema was removed and fixture stopped. This budget code is local/unreleased; terminal snapshot retention, publication and dispatch remain unfinished.

17 October local terminal-retention checkpoint: added bounded, unscheduled `prune_terminal_snapshots`. Old stopped/completed snapshot bodies can be cleared under document/job locks while preserving digests, attempt history and terminal identity. This prevents cleanup from resetting exhausted automatic attempts. Pending/running/recent jobs remain untouched; body cleanup is rollback-safe and leaves READY results and graph checkpoints intact. Admission excludes empty terminal bodies while retaining their metadata tombstones. See [snapshot safeguards](GRAPH_SNAPSHOT_BUDGETS.md). Three offline budget/retention policy tests and the full 465-test suite pass; isolated PostgreSQL runner passes 26 checks. No production data was pruned, no environment changed, no automatic maintenance or dispatch enabled, and these safeguards remain local. Physical-storage limits and cross-store publication/crash reconciliation are not demonstrated.

17 October publication preflight: added pure `prepare_graph_publication` in `app/services/graph_publication_candidate.py`. It requires one complete source execution, validates primary concept chunk/page/upload/filename provenance and optional multi-source references/pages, rejects unsupported or post-validation-mutated graph structure, and freezes an exact candidate JSON/digest including a source-content fingerprint. It accepts valid aggregate graphs beyond provider batch limits. Seven new offline tests pass; full suite 472 tests (7.863 seconds), plus `git diff --check`. No persistence or projection calls occur. This is NOT a publication receipt, semantic validation, lease/fence check or cross-store transaction: future publication must reload authority and handle ambiguous commits/deletion races. It is not connected to runtime projection or deployed; previous 26 PostgreSQL checks do not test this pure helper or claim cross-store safety.

17 October fenced-preflight integration: `GraphContinuationService.prepare_publication` now validates the current independent job claim and persisted snapshot/contract, then calls the preflight using the current document identity. Unlike the extraction input reader, successful preflight keeps document/job locks; caller owns commit/rollback. Two new offline tests verify lost-claim rejection and no implicit commit/rollback on success. The isolated PostgreSQL runner confirms another session's `FOR UPDATE SKIP LOCKED` cannot acquire the held document, can acquire it after rollback, and a stale claim cannot prepare a candidate. All 474 backend tests (10.453 seconds) and 27 PostgreSQL checks pass, zero hosted calls; test schema removed and fixture stopped. The report hashes include candidate code/tests. No remote graph write or job completion occurs: authority expires when the caller releases its transaction. Candidate durability, publication replay/ambiguous commits, hard-kill/deletion reconciliation and bounded dispatch remain open. These changes are local, with no production mutations or new deployment.

17 October strict-provenance regression: a new test reproduced four preflight false acceptances for primary/list page values `True` and `1.0`, because Python equality treats these as equal to integer `1`. The internal preflight now checks exact integer types before serialization/revalidation. All 475 offline backend tests pass (2.989 seconds); `git diff --check` passes. This is local preflight hardening only, with no public schema change, provider call or deployment. The 27-check PostgreSQL report remains evidence for its recorded earlier candidate hashes; it was not regenerated for this pure type-check change. Cross-store publication remains unimplemented.

17 October section-provenance regression: new tests reproduced false acceptance of fabricated primary/plural section headings in the local publication preflight. It now compares headings with referenced source metadata while accepting the ingestion pipeline's existing `Unlabelled section` fallback and legacy empty primary labels for sources without headings. Two additional tests and all 477 backend tests pass (5.632 seconds); `git diff --check` passes. No public schema, source extraction, graph output, production data or environment was changed. The prior PostgreSQL report was not rerun for this pure guard change and retains its original code hashes. Publication remains disconnected and cross-store crash recovery remains open.

18 October local reported-token checkpoint (independent unfinished Phase 3 accounting item): Groq graph and answer adapters now capture the installed SDK response's prompt/completion/total token counts inside the existing measured attempt. A context-local collector is reset in `finally`; missing/invalid counts remain unknown, not fabricated zero usage. Metadata-only reported totals and coverage counts are additive to existing estimated fields, per-provider/per-operation summaries and recent events. Graph responses rejected after generation retain reported usage; nested measurements cannot leak counts into each other. Six new mocked tests include both Groq adapter hooks, validation failure, missing/invalid usage and context isolation. All 483 backend tests pass (7.995 seconds), plus `git diff --check`. No hosted calls, frontend changes, model/routing changes, budget refunds or deployments. Dashboard still displays character-based estimates. Request/token reservations remain conservative estimates; Gemini/Cerebras capture, hosted embeddings/reranking admission, full accounting and cost attribution remain unfinished. Prior PostgreSQL reports retain their recorded earlier hashes, including pre-telemetry ingestion code; no fresh cross-store claim is made.

18 October Gemini usage checkpoint: Gemini REST responses now capture validated `usageMetadata` prompt/candidate/total counts before completion validation, including truncated/empty completions with reported usage. The provider total is preserved rather than reconstructed: [Google's official response reference](https://ai.google.dev/api/generate-content#UsageMetadata) includes thinking/tool-use tokens in total, beyond response candidates. Missing/invalid fields remain unknown. Four new mocked REST tests and all 487 backend tests pass (8.960 seconds); `git diff --check` passes. Documentation connector was out of credits; official reference was retrieved through the regular web reader, without a purchase. Zero hosted model calls, environment/model/routing changes or deployments. Dashboard estimates and estimated admission reservations remain unchanged; no refund/reconciliation or complete billing claim. Cerebras/hosted embedding/rerank capture and full operating-cost attribution remain unfinished, as does publication/crash recovery.

- [ ] Run the API and workers as genuinely separate local processes; verify shared claiming and worker presence.
- [x] Implement and test optional interactive request/token holdbacks across graph failover/repair; production activation is deferred until delayed scheduling is implemented.
- [ ] Durably defer quota-limited graph work until eligible; avoid hot retry loops and unlimited attempts.
- Manual retry eligibility is now persisted/enforced and tested across a fresh process. Automatic delayed work dispatch remains unimplemented; keep the above acceptance item open.
- Durable graph-job metadata supports independent READY-safe claims, snapshot/contract validation, bounded attempts, database-clock deferral and fenced section checkpoint writes. Sixteen real PostgreSQL checks include three claim processes and a SIGKILL claimant with fixture-expired lease. Atomic staging is now wired to READY completion behind a default-off flag, but **not to a coordinator/graph-only processor**; complete automatic continuation remains open.
- [ ] Preserve validated checkpoints and resume only unfinished batches; retain lease fencing and deletion semantics.
- Both section and relationship-recovery checkpoints can now be saved under an independent graph-job fence. Recovery identity is rebuilt from validated saved section output; explicit empty results survive a PostgreSQL round trip and resume without model calls. This is a checkpoint contract, not a complete graph-only processor/publication path; keep the above item open.
- [x] Add estimated token reservations and optional caps alongside request limits; labelled as estimates, not actual provider usage or a hard quota guarantee.
- [ ] Account explicitly for hosted embedding and reranking work and its independent limits. Pre-request admission is wired for Cohere rerank plus Qdrant Cloud document/query embedding paths locally; Cohere's reported rerank search units are measured, but Qdrant usage telemetry and complete billing/cost attribution are still missing.
- [ ] Cache only validated extraction results with content, provider/model, prompt and schema versions; rebind provenance safely and invalidate stale entries.
- Per-upload checkpoints now have source/configuration/prompt/schema fingerprints, strict persisted-output validation and provenance rebinding. Twenty offline checks include serialization into a fresh process. This prerequisite was implemented before automatic graph-job processing because the old checkpoint identities could reuse incompatible model output. Cross-upload cache remains pending; keep the above acceptance item open.
- [ ] Test concurrent reservations, quota reset, failure/cancellation, restart/resume, priority and cache invalidation before deployment.

The initial characterization suite is `tests/test_provider_admission_contract.py`. It makes no database or provider requests. These tests protect existing boundaries; they do not demonstrate separate-process operation or the new priority/token/cache features.

Implementation order for the next run: (1) implement graph-only processing and document/task/job-fenced publication/completion using the internal [transactional projection primitive](GRAPH_PROJECTION_REPLACEMENT.md). Shield the whole fenced Neo4j/PostgreSQL completion unit, reconcile ambiguous commits and test actual-store rollback/crash behavior; the primitive alone is not cross-store atomicity; add bounded dispatch and logical snapshot storage accounting/retention before activation. Section/recovery checkpoint saves and atomic READY/job staging are verified; keep `GRAPH_CONTINUATION_SNAPSHOTS_ENABLED=false` until remaining gates pass. (2) Verify real separate API/ingestion workers, full hard-kill recovery and cross-store consistency; (3) complete hosted usage accounting where providers expose it; (4) cache versioned validated graph batches across uploads while safely rebinding provenance. See [the activation gate](GRAPH_CONTINUATION_JOBS.md#next-implementation-gate). Do not rotate the original READY task or weaken existing document fences. Accepted manual retries still follow the full original pipeline, although completed graph batches are reused. Cohere's comparison path makes two distinct requests; do not count one user question as one universal provider request. Admission/holdback primitives are tested; do not mark Phase 3 complete or claim their zero-default enforcement is active in production.

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

- Bounded-draft scoped release preparation: fresh 505 backend tests pass (3.882 seconds, `/private/tmp`, no project `.env`); all 35 isolated PostgreSQL checks, nine frontend tests, TypeScript/Vite build (Vite 5.86 seconds), compile and diff checks pass. Read-only code review found no blockers for the undispatched draft feature and explicitly did not assess cross-store commit safety or capacity. Schema initialization/reinitialization, READY SQL preservation, draft rollback and fixture deletion cascades are verified; older code can ignore the additive table without dropping it. Zero hosted calls. Staging/dispatch flags and environment remain unchanged; cleanup remains unscheduled. Record the actual pushed/deployed revision separately. This is not Phase 3 completion.

- Resumed local concurrency verification: 35 PostgreSQL checks and 505 backend tests pass; compile/diff checks pass. Both exact disposable directories (`vGlqlb` and the prior stopped `dYAWJZ`) were removed after server shutdown and ownership/PID checks; no user or production data was removed. No new frontend run, commit, push or deployment this increment. Previous frontend/build results retain their historical scope; deployed baseline remains `5034ae6`. The next implementation gate is the distinct late-write-safe intent/receipt and reconciliation protocol, not activation of the cascading draft.

- Concurrency-check preparation follow-up: all 505 offline backend tests pass again (8.841 seconds from `/private/tmp` without project `.env`); compile and diff checks pass. New PostgreSQL concurrency checks could not run because fixture startup approval timed out twice. No new database result, frontend run, release or deployed revision is claimed. Previous 33-check/9-frontend/build results retain their historical scope; drafts remain local and automatic continuation disabled.

- Draft-limit local follow-up: 505 backend tests, 33 isolated PostgreSQL checks, nine frontend tests, TypeScript/Vite build and compile/diff checks pass. Vite reported 11m 57s elapsed; this is not a throughput metric or comparable performance benchmark. Fixture schema removed and server stopped, but removing `/private/tmp/conceptgraph-budget-pg.dYAWJZ` was not executed because automatic approval review was unavailable (model at capacity). Do not treat the remaining stopped directory as an active server or bypass review to remove it. No commit/push/deploy; latest verified deployed revision remains `5034ae6`. Default-zero budgets and unscheduled cleanup are not active production controls.

- Local draft checkpoint verification: nine frontend tests, TypeScript/Vite production build (Vite 5.35 seconds), compile and diff checks pass in addition to 501 backend tests and 31 isolated PostgreSQL checks. No commit/push/deploy for the draft schema this run; current deployment remains `5034ae6`. Temporary fixture schema/server/directory were removed. Continue with draft accounting/retention and late-write-safe publication protocol; do not activate automatic continuation.

- Replay-validation local checkpoint: nine frontend tests and `npm run build` (TypeScript plus Vite, 8.10 seconds for Vite) pass alongside 492 offline backend tests, compile and diff checks. Four scoped files remain local and uncommitted; no release or new production revision is claimed for this helper. Current verified deployed baseline remains `f144b42`; full cross-store publication acceptance is still open.

- Safeguards CI follow-up: release `b3b2f7b` reached public API readiness with no degraded services, but GitHub run `39123447272` failed one mocked Groq test because it relied on a locally configured key. The fixture now supplies an explicit dummy key; production keys and runtime behavior are unchanged. The complete 488-test suite was rerun from `/private/tmp` without loading the project `.env`. Frontend CI passed. Authenticated Render logs remain unavailable until sign-in, and automatic continuation is still disabled.

- 18 October safeguards release verification: all 488 offline backend tests pass (12.269 seconds), including concurrent-thread reported-usage isolation; nine frontend tests and TypeScript/Vite production build pass. A PostgreSQL runner interrupted long enough for its 180-second claim to expire failed its recovery-checkpoint assertion; a fresh uninterrupted run passes all 27 checks with zero hosted calls and removes its randomized schema. Production lease enforcement was not weakened. Snapshot budgets, terminal-body pruning, publication preflight and reported Groq/Gemini usage remain additive/internal; automatic graph continuation stays disabled. Render authentication has expired, so authenticated deployment-log verification needs sign-in; do not infer deployment success from these local checks.

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
