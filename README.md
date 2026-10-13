# ConceptGraph

**A source-grounded GraphRAG application for academic PDFs.** Upload course material, explore an evidence-linked concept map, compare vector and graph-assisted retrieval, and ask questions with page-level citations.

[Live application](https://conceptgraph-frontend.onrender.com/) · [Public, no-login preview](https://conceptgraph-frontend.onrender.com/sample) · [API readiness](https://conceptgraph-api.onrender.com/api/v1/ready) · [CI](https://github.com/Ninjax26/conceptgraph-portfolio/actions/workflows/ci.yml) · [Engineering handbook](ENGINEERING_HANDBOOK.md)

![ConceptGraph dashboard showing the course workspace and concept graph](public/dashboard-preview.jpeg)

> **Deployment scope:** This is a production-deployed **portfolio demo**, not a multi-tenant SaaS. The public preview is read-only and includes prepared examples that work without an API key or available LLM quota. Live uploads and AI requests require a reviewer access code. Reviewers share one workspace; their PDFs are **not private from other reviewers**.

## What it does

- **Ingests PDFs:** validates files, detects duplicates, extracts page-level text, applies bounded OCR to sparse scanned pages, detects headings from PDF layout, and creates overlapping chunks.
- **Builds an explainable graph:** extracts concepts and six allowed relationship types in bounded, resumable batches. Each accepted concept links back to its PDF, page, section, and upload.
- **Answers with evidence:** retrieves course-scoped passages, optionally expands with one- or two-hop prerequisites, reranks results, and cites source pages. It refuses unsupported questions instead of inventing an answer.
- **Handles imperfect AI output:** validates schema, relationship endpoints, source references, and graph quality. A searchable document can be marked graph-ready, graph-partial, or ready-without-graph.
- **Supports a controlled demo:** provides a no-login saved sample, reviewer sessions and rate limits, citation-backed practice questions, document retry/deletion, and automatic reviewer-upload retention.

## Try it

1. Open the [public preview](https://conceptgraph-frontend.onrender.com/sample) to inspect saved questions, answers, citations, and a sample graph. These prepared examples require no login or live provider call.
2. Open the [live dashboard](https://conceptgraph-frontend.onrender.com/dashboard) if you have a reviewer code. Upload a PDF, wait for its processing state, then select a course and ask a question.
3. Run the same question in **Vector only**, **One hop**, and **Two hops** to compare evidence and latency. Click a graph node or citation to inspect its source page.

The API and providers run on quota-limited hosting. A cold start or exhausted free-tier quota can delay or limit live requests; the saved preview remains available.

### Prepared public samples

The `/sample` page offers two courses without authentication or live AI requests:

- **Computing Foundations:** the original three-PDF course covering web protocols, identity/security, and data reliability.
- **Equipment Documentation:** three fictional PDFs (specification, maintenance guide, inspection record), four saved questions, and explicit source-linked `PART_OF` / `APPLIES_TO` illustrations. Three questions require evidence from different documents; one demonstrates an unsupported motor-power refusal. One inspection page is image-only to exercise OCR. Document revisions and page/paragraph references are visible, but this is not automatic revision reconciliation or equipment operating advice.

Select a course, open its cited pages, or download the source PDFs to try the actual ingestion pipeline in the protected reviewer workspace. `?course=technical` selects the equipment sample directly. Saved answer text and graph edges are **manually authored references**, not cached live retrieval results, measured LLM performance, or a claim that every upload will produce the illustrated graph.

Regenerate and check fixtures locally:

~~~bash
python scripts/build_sample_course.py
python scripts/verify_public_samples.py
~~~

Use `--technical-only` when rebuilding only the equipment files. Verification requires local Tesseract English data and checks actual native/OCR extraction plus source references. The [recorded check](evaluation/public-samples-verification.json) recovered all authored reference paragraphs on **24/24 pages**, including the image-only page. That page's 95.7/100 engine confidence is not an accuracy percentage. This small authored-English check does not measure retrieval, answer correctness, graph extraction, or real-world scanned documents. These additions remain local until deployed.

## Architecture

~~~mermaid
flowchart LR
  Browser[React / Vite / Cytoscape] --> API[FastAPI]
  API --> PG[(PostgreSQL: courses, jobs, leases)]
  API --> Store[(Private object storage: PDFs)]
  API --> V[(Qdrant: passage vectors)]
  API --> G[(Neo4j: concepts and edges)]
  API --> Rank[Cohere or local reranker]
  API --> AI[Groq primary / Gemini fallback]
  API -. combined mode .-> Worker[Ingestion worker: combined or separate]
  Worker --> PG
  Worker --> Store
  Worker --> V
  Worker --> G
  Worker --> AI
~~~

PostgreSQL is the **source of truth** for uploads, stages, attempts, and leases. The in-memory queue only accelerates dispatch: an accepted upload is persisted before processing, and an expired lease can be recovered after a restart. Qdrant, Neo4j, and object storage have separate roles rather than storing the same state four times. The hosted profile uses Qdrant Cloud inference and Cohere reranking so the API does not have to keep local ML models in memory.

| Component | Responsibility |
| --- | --- |
| React, TypeScript, Vite, Cytoscape.js | Dashboard, retrieval-mode comparison, and interactive concept map |
| FastAPI and PostgreSQL | HTTP API, durable workflow, attempts, leases, and recovery |
| PyMuPDF and Tesseract | Native PDF extraction, layout-aware headings, and scanned-page OCR fallback |
| Qdrant | Metadata-filtered semantic passage search |
| Neo4j | Typed, provenance-linked concept relationships and bounded traversal |
| Private S3-compatible storage | Source PDFs; Cloudflare R2 on the hosted deployment, MinIO locally |
| Groq, Gemini, Cohere | Graph/answer generation with failover; passage reranking |

### Document lifecycle

~~~text
Upload → validate/deduplicate → store PDF → extract text/OCR
       → detect sections and chunk → embed in Qdrant
       → extract/validate/checkpoint graph batches → project to Neo4j
       → READY + GRAPH_READY / GRAPH_PARTIAL / READY_WITHOUT_GRAPH
~~~

Native text is preferred. OCR can recover sparse or corrupted text layers and supplement readable pages containing scanned text in large image regions. Output must pass text-quality and engine-confidence checks; more characters alone are insufficient. Orientation-aware OCR and one optional alternate pass share a page deadline and rendering budget. The dashboard flags pages needing review. Native heading detection uses font size, weight, numbering, length, and spacing; scanned text uses a text-only fallback. Graph extraction samples across sections, preserves completed batches, and stops at a configured budget.

A document can be **READY** for vector search even when no valid graph was produced. The graph status is displayed separately; an empty graph is not presented as a successful extraction.

Processing details distinguish malformed output, invalid relationships, unverified sources, truncated responses, quota, and timeouts. Explicit empty responses are checkpointed and reported separately from failures. Structural failures may use one additional bounded repair call after provider failover (`GRAPH_JSON_REPAIR_ENABLED`, enabled by default); that call still passes source and schema validation. Repair attempts appear in provider metrics.

### Query path

~~~text
Question + READY course
  → optional scoped Neo4j prerequisite traversal (0, 1, or 2 hops)
  → Qdrant passage search + optional scoped identifier lookup
  → bounded rerank windows → evidence/identifier checks
  → grounded refusal OR answer with page-level citations
~~~

Graph traversal is bounded and uses parameterized, read-only queries; the LLM does not write Cypher. If Neo4j is unavailable, graph-assisted modes fall back to vector retrieval. Provider quota errors and timeouts trigger a cooldown and a Groq-to-Gemini attempt where configured. If generation is unavailable but usable evidence exists, the answer path can return the retrieved passages rather than fabricate prose. Provider status records attempt counts, outcomes, latency, and estimated tokens **without storing prompts, answers, document names, or keys**.

Retrieval keeps a configurable, bounded shortlist (10 by default). Questions containing identifiers can supplement dense search with a READY-scoped keyword lookup and rank fusion, without another embedding or LLM call. Named identifiers must be verified in the cited passages before generation; high similarity alone cannot justify answering about nonexistent equipment. Question-focused windows avoid always cutting citations and rerank input at the start of a long chunk. Legacy uploads remain vector-searchable and gain identifier payload data when reprocessed.

## Measured results

The dashboard distinguishes a graph timeout, an unavailable graph service, and recorded graph counts with no matching projection in the selected Neo4j database. Vector fallback preserves the actual retrieval mode and citations. Query diagnostics now split collection checks, local query embedding, dense search, identifier lookup, reranking, and generation; hosted embedding executes inside dense search and is labelled accordingly. These timings do not record prompts or document text.

The latest [provider-generated retrieval ablation](evaluation/ablation-provider-current.md) ran **22 labelled questions across three authored PDFs**: 17 supported and 5 unsupported. It produced 61 validated concepts and 44 relationships and made 66 retrieval requests. Answer generation was deliberately excluded so the comparison measures retrieval rather than LLM wording.

| Measure | Vector only | One hop | Two hops |
| --- | ---: | ---: | ---: |
| All required sources in raw top five | 16/17 | 17/17 | 17/17 |
| All required sources after the evidence gate | 12/17 | 12/17 | 12/17 |
| Unsupported questions refused | 5/5 | 5/5 | 5/5 |
| Average retrieval latency | 0.06 s | 0.48 s | 0.41 s |

**Interpretation:** Graph expansion improved one raw multi-source retrieval case, but did **not** improve the final evidence-gated score in this small fixture. It added latency. Two-hop terms were used in only three questions, so this is not evidence that deeper traversal is generally better. The [15-question single-course baseline](evaluation/baseline-2026-08-30.json), [multi-document labels](evaluation/questions-multidoc.json), and [full ablation report](evaluation/ablation-provider-current.md) preserve the misses and methodology.

A separate [authenticated production smoke result](evaluation/production-smoke-current.json) checked health, dependency readiness, course discovery, one grounded query, and the matching provider-attempt increment on a deployed release. It exercised the healthy Groq route; **production failover was not forced** merely to burn through a quota. Graph, answer, and exam failover are covered by deterministic tests. A smoke test is not a load test or an answer-quality benchmark.

The [local OCR check](evaluation/ocr-quality-current.md) compares the previous extractor with the updated pipeline on eight authored English fixtures using real Tesseract. Upside-down, mixed-content, and corrupted-hidden-text fixtures improved; native and other scanned fixtures retained their results. Timings vary with layout and runtime conditions; the report lists each fixture's measured cost, not a general speedup claim. This small synthetic check does not establish accuracy on customer scans, Japanese text, handwriting, tables, or drawings. Reproduce it with `python scripts/evaluate_ocr.py --baseline 5996dce03ec3d4e43b86b3afc1837dbbcf2f1870`.

The [retrieval scaling check](evaluation/retrieval-scaling-current.md) runs 45 authored questions against 10, 50, and 100 PDF corpora with competing document identifiers. It measures retrieval, reranking, and evidence selection separately, including MRR@5, source recall, page-label precision, refusals, and stage latency. Development and diagnostic validation cases are distinguished from fresh authored held-out cases. It uses cached local models and in-memory Qdrant, with no LLM generation or production writes. These results do not establish hosted ANN performance, concurrent-user capacity, or an increased production upload limit. Reproduce it with `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 python scripts/evaluate_retrieval_scaling.py`.

Increasing the dense shortlist from 10 to 20 did not improve evidence completeness in that fixture and increased local reranking time, so the default remains 10. Identifier enrichment recovered one multi-source miss in the larger corpora; identifier verification refused unsupported equipment queries that otherwise passed the evidence gate. Six supported questions still lacked all labelled sources after filtering. The evidence threshold remains unchanged; these small synthetic results do not calibrate hosted Cohere or prove answer correctness.

The subsequent [Phase 1 evidence-selection check](evaluation/evidence-selection-phase1.md) recovers two of those six misses using bounded literal sentence windows and complementary evidence that need not repeat an identifier. At each tested corpus size, required-source completeness increases from **29/35 to 31/35**, while unsupported refusals remain **10/10**. Development improves from 12/17 to 14/17; diagnostic validation remains 9/10 and the separate authored held-out set remains 8/8. The threshold stays at 0.35. Extra windows add reranker work (up to three inputs per candidate, batched in one hosted request); these local results do not establish hosted quality, cost, or answer accuracy. Four misses remain. The implementation is included in the deployed Phase 1–2 release; these measurements remain offline.

The [Phase 1 continuation](evaluation/evidence-selection-followup-notes.md) audits those four misses and fixes two citation-boundary cases: accepted evidence for a second requested identifier being crowded out of five citation slots, and a conflicting identifier inside the selected excerpt. Final local counts remain **31/35** and **10/10**; nine additional focused regressions pass. Original labels and earlier reports are preserved, and no additional aggregate accuracy gain or production deployment is claimed.

A further [context and challenge check](evaluation/evidence-selection-context-notes.md) aligns scored and cited text at 900 characters and keeps preceding context in final-sentence windows. The original benchmark still reports 31/35 source completeness and 10/10 evidence-gate refusals, but a separate harder set reports **4/6 source completeness and 2/4 gate refusals**. Missing comparison evidence and absent equipment attributes remain limitations. These are retrieval-only checks, not generated-answer accuracy; Phase 1 is not fully closed on broader evidence quality.

The [Phase 1 integration](evaluation/phase1-integration.md) now checks both sides of simple `Compare/Contrast A with B` questions before synthesis. Development source completeness improves from **2/3 to 3/3**; a fresh authored holdout retains **4/4** required source sets and rejects **2/2** unsupported comparisons across 10/50/100 PDFs. Ordinary questions remain single-query; matched hosted comparisons require two Cohere rerank requests. The original 31/35 and 10/10 results are unchanged, and missing-attribute checks remain a limitation. This integration is included in the deployed Phase 1–2 release; hosted answer accuracy remains unmeasured. See the [implementation ledger](docs/IMPLEMENTATION_PROGRESS.md) for release verification and the remaining roadmap.

### Answer and graph quality (offline)

[Phase 2 quality checks](evaluation/quality-evaluation.md) add twelve claim-review cases, citation index/page/excerpt validation and graph type/direction/provenance audits. The deterministic fallback baseline has **20/20 valid citation indices** and **21/21 literal excerpts**; an older saved graph contains **6/6 known prerequisite links**. These are not LLM accuracy metrics. Groundedness, semantic citation correctness, completeness and refusal accuracy remain **unmeasured pending captured answers and explicit reviews**. The checks make no provider calls or production-data changes.

```bash
.venv/bin/python scripts/evaluate_quality.py --fallback
```

## Reliability and security boundaries

OCR uses a bounded child-process thread count (`OCR_THREADS=1` by default). The free Render profile gives the existing per-page OCR/retry deadline 60 seconds without lowering confidence/text-quality thresholds. A live smoke test exposed an OCR timeout despite healthy readiness; the [constrained profile check](evaluation/ocr-free-profile.md) records the failure and measurement limits explicitly.

- **Durable processing:** a bounded one-worker default, PostgreSQL leases/heartbeats, task-token fencing, restart recovery, retries, and idempotent cleanup protect against lost or stale work.
- **Data lifecycle:** deletion removes upload-scoped data from PostgreSQL, Qdrant, Neo4j, and object storage. Reviewer uploads expire after the configured retention period; the public sample is excluded.
- **Protected operations:** a reviewer code is exchanged for an expiring, signed HttpOnly cookie and verified before the dashboard unlocks. Upload, query, exam, retry, deletion, and provider metrics require authorization. The static sample needs none.
- **Explicit limits:** the hosted blueprint caps PDFs at 10 MiB, defaults to one processor, and uses rate limits and bounded graph/OCR work. Secrets belong only in the API environment, never in frontend Vite variables or Git.
- **Honest scope:** reviewers share data and a code. Process-local rate limits, cooldowns, and metrics reset on restart. This is suitable for a controlled portfolio demo, **not** private per-user storage, horizontal scaling, or an unrestricted public upload service.

## Run locally

Requirements: **Python 3.12, Node.js 22, Docker**, and at least one configured LLM key for live graph generation/answers. The local profile uses local embedding and reranking models; first-time model downloads require network access and more memory than the hosted profile.

~~~bash
cp .env.example .env
# Edit .env: supply a Groq key or another supported provider key.
docker compose up -d

python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements-local-models.txt
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
~~~

In another terminal:

~~~bash
npm ci
npm run dev -- --host 127.0.0.1
~~~

Open [http://127.0.0.1:5173](http://127.0.0.1:5173). Docker Compose starts PostgreSQL, Qdrant, Neo4j, and MinIO; the API includes the background processor. There is no Redis or separate Celery worker. For direct local OCR, install Tesseract and its English language data. The [API Dockerfile](Dockerfile.api) includes them for the deployed container.

Do not commit .env or paste credentials into an issue, screenshot, README, or VITE_* variable. See [.env.example](.env.example) for the complete configuration reference.

## Deploy

[render.yaml](render.yaml) defines one Docker API service and one static frontend. It expects **external PostgreSQL, Qdrant, Neo4j, private S3-compatible storage, and provider credentials**; the Blueprint does not create these accounts or a database for you.

1. Provision the backing services and private PDF bucket. Set the Blueprint's secret values in Render, including DATABASE_URL, QDRANT_*, NEO4J_*, S3_*, GROQ_API_KEY, optional GEMINI_API_KEY, COHERE_API_KEY, and a long DEMO_ACCESS_TOKEN.
2. Set ALLOWED_ORIGINS to the exact frontend origin and VITE_API_BASE_URL to the deployed API base. Never expose a backend secret as a frontend environment variable.
3. Configure PUBLIC_SAMPLE_COURSE_ID to an existing READY course after uploading its sample PDF. Confirm [readiness](https://conceptgraph-api.onrender.com/api/v1/ready), the public preview, one protected upload, citations, and deletion.
4. Back up PostgreSQL before a rollout. The API applies additive, idempotent schema changes at startup; roll back the image first, without deleting stored PDFs, vectors, or graph data.

The Render API uses a low-memory hosted inference profile. Free plans and provider quotas can change or pause; this deployment is not an uptime or cost SLA. See the [engineering handbook](ENGINEERING_HANDBOOK.md) for implementation invariants and the [Blueprint](render.yaml) for exact service settings.

## Scaling profile — local implementation

The scalable profile keeps the existing PostgreSQL jobs, leases, task-token fencing, and graph checkpoints. It introduces no Redis or Celery dependency. `PROCESSING_ROLE=combined` preserves the original single-process deployment; `api` accepts durable uploads without processing them, and `worker` runs ingestion separately. Multiple workers use independent instance IDs and PostgreSQL row locks to claim work safely. All updates to a current attempt hold its row lock through the transaction, and concurrent schema initialization is serialized.

Upload admission checks pages, reserved chunks, source PDF bytes, and pending jobs under the existing installation advisory lock. Actual chunks reconcile the reservation **before** vector indexing. Chunk generation streams through word boundaries and stops at the document limit instead of building an unbounded chunk list. Ordinary pipeline stages, eligibility of READY documents, retry checkpoints, and compensating deletion remain unchanged.

| Setting | Default | Meaning |
| --- | ---: | --- |
| `MAX_PAGES_PER_DOCUMENT` / `MAX_PAGES_PER_INSTALLATION` | 500 / 10,000 | Per-PDF and retained-corpus page budgets |
| `MAX_CHUNKS_PER_DOCUMENT` / `MAX_CHUNKS_PER_INSTALLATION` | 2,000 / 50,000 | Per-PDF and installation chunk reservations |
| `CHUNK_RESERVATION_PER_PAGE` | 32 | Initial estimate, reconciled before indexing |
| `MAX_STORAGE_MB` | 1,024 | Source PDF byte budget; not total database/vector/graph storage |
| `MAX_PENDING_DOCUMENTS` | 32 | Durable accepted jobs waiting or processing |
| `PROCESSING_CONCURRENCY` / `PROCESSING_QUEUE_CAPACITY` | 1 / 8 | Per-worker execution and dispatch bounds |
| `GRAPH_DISPLAY_MAX_NODES` / `GRAPH_DISPLAY_MAX_EDGES` | 100 / 200 | Unique nodes/edges in the returned visual subset |

The legacy `MAX_PDFS_PER_INSTALLATION=50` remains an optional compatibility guard. Set it to **0** to rely on resource budgets; the local launcher does so without editing `.env`. OCR/rendering and graph-batch budgets still apply. These are application admission/work bounds, not a hard operating-system memory sandbox or a guarantee of a hosting plan's capacity.

Split operation requires `SHARED_PROVIDER_LIMITS_ENABLED=true` on API and workers. Active graph, answer, and exam calls reserve application-wide per-provider request capacity in PostgreSQL using database time: defaults are **10/minute and 300/UTC day** for each generation provider. Use the same database, `SHARED_PROVIDER_SCOPE`, provider configuration, and budget settings across processes. Failed or cancelled reservations consume capacity conservatively. A shared-application limit is distinguished from an actual vendor-quota error in processing details. These guards do **not** meter provider tokens/billing, Cohere/Qdrant inference, or usage of the same keys in another project/database. Existing HTTP/login rate limits remain process-local: this profile supports one protected API with additional workers, not unrestricted public API replication.

The [Phase 3 admission increment](docs/PROVIDER_ADMISSION.md) adds persisted **estimated-token reservations** and opt-in interactive request/token holdbacks. Zero defaults preserve existing request-limit enforcement. Application-budget denials no longer open vendor circuit breakers. [Ten isolated PostgreSQL checks](evaluation/provider-admission-current.md) cover additive migration, old-release SQL, three-process contention, cancellation, rollback and resets. These are admission-contract tests, not a separate ingestion-worker or provider-capacity benchmark. Actual vendor tokens, hosted embedding/reranking admission, automatic graph deferral and cross-upload caching remain unfinished.

The [retry-eligibility increment](docs/GRAPH_RETRY_ELIGIBILITY.md) stores quota reset times and rejects premature manual graph retries without spending attempts or disturbing READY evidence/checkpoints. It also fixes stale ORM state bypassing concurrent retry/claim/deletion decisions despite row locking. [Ten isolated PostgreSQL/API checks](evaluation/graph-retry-current.md) pass. Automatic graph continuation and a graph-only processing path remain pending; accepted retries still run the original pipeline and reuse completed graph batches.

[Versioned per-upload graph checkpoints](docs/GRAPH_CHECKPOINT_COMPATIBILITY.md) include the source, configured model route, prompt and schema in their identities, revalidate saved relationships before normalization, and rebind provenance to current chunks. [Twenty offline checks](evaluation/graph-checkpoint-compatibility-current.md) verify compatibility, invalidation and quota resume without provider calls. Old unversioned rows are retained but may require fresh bounded extraction on a requested rebuild. This is not yet a cross-upload cache or automatic graph-only continuation.

The [durable graph-job foundation](docs/GRAPH_CONTINUATION_JOBS.md) adds independent PostgreSQL leases, source-bound snapshots, bounded attempts/due times and section-checkpoint fencing without changing READY evidence identity. [Sixteen isolated database checks](evaluation/graph-continuation-jobs-current.md) cover concurrent claim processes, a killed metadata claimant, retention and SQL deletion cascade. **It is not yet connected to a scheduler or graph-only processor; automatic production continuation remains disabled.** These checks do not establish full ingestion-worker recovery or cross-store consistency.

The dashboard loads 25 documents per page, with Previous/Next controls and page-local filters/counts. The original array-returning upload endpoint remains compatible. The new keyset endpoint deduplicates canonical records in SQL; it bounds client memory but its ranking step can still scan the corpus. Graph display limits do not make every Neo4j traversal constant-time.

Run separate local processes, using the existing local PostgreSQL and Neo4j containers:

~~~bash
python scripts/start_local.py --role api --local-neo4j --port 8001
# In another terminal:
python scripts/start_local.py --role worker --local-neo4j
# Frontend terminal:
VITE_API_BASE_URL=http://127.0.0.1:8001/api/v1 npm run dev -- --host 127.0.0.1 --port 5174
~~~

The launcher binds the API to loopback, refuses a remote PostgreSQL database, uses the existing local Neo4j credential only in process memory, and leaves `.env` unchanged. Other backing services still use `.env`; this is not automatically a fully offline stack. For a conventional deployment, set the same role/shared-budget variables and run `python -m uvicorn app.main:app` or `python -m app.worker`. No hosting resources or production configuration have been changed for this profile.

Changing the Neo4j target does not copy previously stored graphs. Existing READY rows may still describe a graph in another Neo4j instance; verify the projection in the selected instance before treating those counts as a local graph check. Rebuilds are explicit operations, not an automatic migration performed by the launcher.

`GET /api/v1/ready` reports worker availability. An offline worker degrades ingestion while existing READY queries remain usable; accepted jobs remain durable. Protected `GET /api/v1/ingest/resources` reports usage/limits. Unknown legacy page/byte metadata is counted conservatively. Inspect it without AI/index writes using `python scripts/refresh_resource_inventory.py`; `--apply` fills metadata only in local PostgreSQL. Review the dry run first.

The [ingestion scaling report](evaluation/ingestion-scaling-current.md) measures 10/50/100 authored PDFs, including pages, chunks, source bytes, queue wait, indexing, retrieval, admission backpressure, pagination, restart/checkpoint recovery, stale-attempt fencing, shared request reservations, and deletion cleanup. Reproduce it with:

| Fixture PDFs | Pages / chunks | Ingestion wall time | Queue wait p95 | Warm vector retrieval p95 |
| ---: | ---: | ---: | ---: | ---: |
| 10 | 20 / 20 | 30.333 s | 17.748 s | 350.9 ms |
| 50 | 100 / 100 | 101.913 s | 62.141 s | 121.7 ms |
| 100 | 200 / 200 | 204.029 s | 87.813 s | 592.8 ms |

All fixture uploads eventually became READY, with zero duplicate executions. Admission backpressure produced retryable HTTP 429 responses (274 at 50 PDFs and 1,175 at 100 PDFs); the runner retried those admissions. These are deliberately small two-page PDFs, not arbitrary large documents. Five warm retrieval samples are too few to infer a latency improvement as the corpus grows. The longer queue wait is the main measured ingestion bottleneck in this run. This latest run was slower than an earlier local run; host contention and cold-start conditions are not controlled, so the timings are observations rather than a speed or capacity guarantee.

~~~bash
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 python scripts/evaluate_ingestion_scaling.py --documents 10 50 100 --workers 2
~~~

The benchmark uses isolated local PostgreSQL, the real ASGI upload/delete handlers, cached CPU embeddings, and in-memory Qdrant. Worker coordinators share a Python process; storage and graph generation/projection are test doubles. Graph-provider calls are **zero because generation is disabled**, not because graphs are free. It does not establish real Neo4j/R2 consistency under hard kills, hosted ANN performance, actual OS-process memory use, live AI throughput, or production capacity. Private workspace ownership is a separate next phase; reviewer data remains shared.

The [local verification record](evaluation/scaling-validation.md) lists the 299 passing backend tests, eight frontend status tests, build checks, browser checks, and unverified production boundaries. Private workspaces are deliberately deferred; the reviewer demo remains shared.

## Verify

The local launcher selects CPU inference with bounded CPU threading for reproducibility. Manual starts keep `LOCAL_MODEL_DEVICE=auto` by default; select `cpu` or `mps` explicitly if needed. Hosted Qdrant/Cohere inference is unaffected. Device selection is configuration, not an assertion that a specific accelerator caused an observed stall.

~~~bash
python -m compileall -q app tests
python -m unittest discover -s tests -v
npm ci
npm run test:frontend
npm run build
docker compose config -q
~~~

[GitHub Actions](.github/workflows/ci.yml) runs the backend and frontend checks on repository changes. The tests concentrate on failure boundaries: OCR, graph validation, empty/partial graphs, provider errors and failover, durable processing, auth/session verification, citation links, and deletion.

## Repository guide

| Path | Start here for |
| --- | --- |
| [Dashboard](src/pages/Dashboard.tsx) and [graph canvas](src/components/ConceptGraphCanvas.tsx) | Reviewer workflow, answers, citations, and visualization |
| [API endpoints](app/api/endpoints/) | HTTP authorization and request boundaries |
| [Processing coordinator](app/core/processing_coordinator.py) and [document processor](app/services/document_processing_service.py) | Durable jobs, retries, and stage transitions |
| [Parser](app/services/parser_service.py) and [ingestion](app/services/ingestion_service.py) | PDF/OCR/chunking and validated graph construction |
| [Retrieval](app/services/rag_service.py) and [synthesis](app/services/synthesis_service.py) | Vector/graph search, evidence gating, and answers |
| [Evaluation](evaluation/) and [scripts](scripts/) | Labelled datasets, ablation, production smoke, and repeatable checks |
| [Engineering handbook](ENGINEERING_HANDBOOK.md) | Deeper design decisions, operational invariants, and safe changes |

## Limitations and next steps

This implementation does not isolate reviewers from one another. Tesseract recovers text from many scanned pages, but complex tables, diagrams, handwriting, and equation-to-LaTeX conversion are outside scope. Simple concept-name normalization cannot reliably merge synonyms or distinguish homonyms. The evaluation sets are small and authored, and live behavior depends on external service availability.

The next evidence-driven improvements are a broader, independently labelled multi-subject evaluation; measured graph-quality tests on scanned and complex layouts; and real user/tenant isolation **only if** the demo becomes a shared product. The [engineering handbook](ENGINEERING_HANDBOOK.md) documents the current design in more detail.

---

Developed in the separate [conceptgraph-portfolio repository](https://github.com/Ninjax26/conceptgraph-portfolio), based on the [original ConceptGraph project](https://github.com/Ninjax26/conceptgraph). Portfolio changes are not pushed to the original repository.
