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
  API --> Worker[Bounded in-process processor]
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

Native text is preferred. OCR runs only on pages with too little readable native text, and its output is retained only when it recovers more content. For native pages, heading detection scores font size, weight, numbering, length, and spacing; scanned pages use a text-only fallback. Graph extraction samples across sections, preserves valid completed batches, and stops at a configured request budget so one large PDF cannot consume unlimited provider quota.

A document can be **READY** for vector search even when no valid graph was produced. The graph status is displayed separately; an empty graph is not presented as a successful extraction.

### Query path

~~~text
Question + READY course
  → optional scoped Neo4j prerequisite traversal (0, 1, or 2 hops)
  → Qdrant passage search → rerank → evidence threshold
  → grounded refusal OR answer with page-level citations
~~~

Graph traversal is bounded and uses parameterized, read-only queries; the LLM does not write Cypher. If Neo4j is unavailable, graph-assisted modes fall back to vector retrieval. Provider quota errors and timeouts trigger a cooldown and a Groq-to-Gemini attempt where configured. If generation is unavailable but usable evidence exists, the answer path can return the retrieved passages rather than fabricate prose. Provider status records attempt counts, outcomes, latency, and estimated tokens **without storing prompts, answers, document names, or keys**.

## Measured results

The latest [provider-generated retrieval ablation](evaluation/ablation-provider-current.md) ran **22 labelled questions across three authored PDFs**: 17 supported and 5 unsupported. It produced 61 validated concepts and 44 relationships and made 66 retrieval requests. Answer generation was deliberately excluded so the comparison measures retrieval rather than LLM wording.

| Measure | Vector only | One hop | Two hops |
| --- | ---: | ---: | ---: |
| All required sources in raw top five | 16/17 | 17/17 | 17/17 |
| All required sources after the evidence gate | 12/17 | 12/17 | 12/17 |
| Unsupported questions refused | 5/5 | 5/5 | 5/5 |
| Average retrieval latency | 0.06 s | 0.48 s | 0.41 s |

**Interpretation:** Graph expansion improved one raw multi-source retrieval case, but did **not** improve the final evidence-gated score in this small fixture. It added latency. Two-hop terms were used in only three questions, so this is not evidence that deeper traversal is generally better. The [15-question single-course baseline](evaluation/baseline-2026-08-30.json), [multi-document labels](evaluation/questions-multidoc.json), and [full ablation report](evaluation/ablation-provider-current.md) preserve the misses and methodology.

A separate [authenticated production smoke result](evaluation/production-smoke-current.json) checked health, dependency readiness, course discovery, one grounded query, and the matching provider-attempt increment on a deployed release. It exercised the healthy Groq route; **production failover was not forced** merely to burn through a quota. Graph, answer, and exam failover are covered by deterministic tests. A smoke test is not a load test or an answer-quality benchmark.

## Reliability and security boundaries

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

## Verify

~~~bash
python -m compileall -q app tests
python -m unittest discover -s tests -v
npm ci
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
