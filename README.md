# ConceptGraph Portfolio Edition

ConceptGraph turns course PDFs into a searchable concept graph, grounded answers, and citation-backed mock exams. This portfolio edition keeps the React experience and GraphRAG behavior while running the API and background processor in one bounded, restart-safe FastAPI service. Redis and Celery are not required.

[Live demo](https://conceptgraph-frontend.onrender.com/) · [Source code](https://github.com/Ninjax26/conceptgraph-portfolio) · [API health](https://conceptgraph-api.onrender.com/api/v1/health) · [API readiness](https://conceptgraph-api.onrender.com/api/v1/ready) · [CI](https://github.com/Ninjax26/conceptgraph-portfolio/actions/workflows/ci.yml) · [Engineering handbook](ENGINEERING_HANDBOOK.md)

> Portfolio scope: this is a controlled, shared reviewer demo—not a multi-tenant SaaS. Public visitors can inspect one pre-uploaded read-only course. A reviewer access code protects uploads and paid AI operations.

![ConceptGraph dashboard preview](public/dashboard-preview.jpeg)

## Documentation guide

- Start with [What the project demonstrates](#what-the-project-demonstrates) and [Current implementation status](#current-implementation-status) for a portfolio overview.
- Read [End-to-end processes](#end-to-end-processes) for the complete upload, GraphRAG, exam, recovery, authentication, and cleanup flows.
- Use [Local development](#local-development), [Configuration](#configuration), and [Deployment](#deployment) to run or host it.
- Review [Small retrieval evaluation](#small-retrieval-evaluation) and [Verification](#verification) for evidence that the system works.

## What the project demonstrates

- A complete PDF-to-GraphRAG pipeline, not only an LLM chat screen.
- Polyglot persistence with a clear owner for each kind of data.
- Durable background processing on a small single-service deployment.
- Evidence gating, citations, source-page provenance, and honest empty/partial graph states.
- Provider quota resilience with Groq-to-Gemini failover, circuit breaking, and grounded degradation.
- Data lifecycle management: retry, retention, full deletion, and partial-write compensation.
- Measured retrieval results and failure-focused tests instead of an unverified “it works” claim.

## Current implementation status

| Area | Implemented behavior |
| --- | --- |
| Dashboard presentation | Non-overlapping protected-session bar, answers directly under questions, Markdown answer rendering, clearer confidence/citation cards, collapsible processing details, responsive graph layout |
| PDF ingestion | Type/signature/size/password checks, SHA-256 deduplication, conditional OCR, layout-aware headings, private content-addressed object storage, durable upload record before background dispatch |
| Processing reliability | Bounded in-process queue, PostgreSQL leases, heartbeats, task-token fencing, startup recovery, separate failure/graph-continuation limits, safe public error categories |
| Graph quality | Section-aware beginning/middle/end sampling, resumable per-batch checkpoints, one bounded cross-section linking pass, six allowed relationship types, endpoint validation, normalized concept deduplication, partial/empty graph detection |
| Provenance | Upload ID, PDF filename, page, section, and source chunk retained on graph concepts; graph nodes open the original source page |
| Retrieval and answers | READY-only course filtering, graph-assisted Qdrant retrieval, Cohere/local reranking, evidence thresholds, grounded refusal, citations, formatted answers |
| LLM resilience | Groq primary, Gemini failover for graphs/answers/exams, optional legacy Cerebras support, five-minute circuit-breaker cooldown, evidence-only answer fallback |
| Demo security | One reviewer code, signed expiring HttpOnly session cookie, post-login session verification, exact CORS configuration, protected expensive routes, rate limits |
| Public access | One pre-uploaded read-only sample course and source previews without exposing upload/query/exam controls |
| Data cleanup | Manual deletion across PostgreSQL, Qdrant, Neo4j, and R2; automatic demo retention; sample-course exclusion; shared-object protection |
| Evaluation | Fifteen manually annotated questions, committed baseline results, top-five document/page checks, citation/refusal checks, latency measurement |
| Verification | 161 focused backend tests plus Python compilation, TypeScript checking, Vite production build, and Docker Compose validation |
| Scanned PDFs | Sparse pages fall back to bounded local Tesseract OCR with page provenance and confidence reporting |

The commit history records these improvements as separate phases: dashboard polish, answer formatting, cross-site sessions, complete deletion, demo hardening/public sample/retention, evaluation, graph quality and provenance, complex-PDF batching, checkpointed continuation, quota-safe degradation, and provider failover.

## Architecture

```mermaid
flowchart LR
  UI[React + Vite] --> API[FastAPI API]
  API --> PG[(PostgreSQL\ndurable jobs + leases)]
  API --> Q[(Qdrant Cloud\nvectors + inference)]
  API --> N[(Neo4j Aura\nconcept graph)]
  API --> R2[(Private R2 bucket\nsource PDFs)]
  API --> LLM[Groq primary\ngraph extraction + answers]
  LLM -. quota, timeout, or invalid structure .-> CB[Gemini failover]
  API --> RR[Cohere\nreranking]
  API --> C[Bounded in-process coordinator]
  C --> PG
  C --> R2
  C --> Q
  C --> N
  C --> LLM
```

PostgreSQL is authoritative. The in-memory queue is only an execution accelerator: if it is full, a request is saved as `UPLOADED` and the dispatcher picks it up later. If the process stops, its lease expires and startup recovery creates a new, auditable processing attempt. Qdrant, Neo4j, and object storage are derived or external stores; no job state depends on process memory.

### Why each datastore exists

| Store | Responsibility | Why it is not interchangeable |
| --- | --- | --- |
| PostgreSQL | Courses, uploads, stages, attempts, leases, counts, failure state | Transactional source of truth and recovery authority |
| Qdrant | Chunk embeddings and metadata-filtered semantic retrieval | Efficient similarity search over PDF passages |
| Neo4j | Concepts, typed relationships, and source provenance | Natural traversal and visualization of prerequisite/part-of structure |
| R2/MinIO | Immutable private source PDFs | Durable binary storage independent of Render's ephemeral filesystem |

This is deliberate polyglot persistence: every derived Qdrant point and Neo4j entity can be rebuilt from the PostgreSQL record plus the source PDF.

## End-to-end processes

### 1. Upload and document processing

```mermaid
sequenceDiagram
  participant U as Reviewer
  participant API as FastAPI
  participant PG as PostgreSQL
  participant O as R2 / MinIO
  participant W as Coordinator
  participant Q as Qdrant
  participant G as Groq / Gemini
  participant N as Neo4j

  U->>API: Upload PDF + course ID
  API->>API: Validate file and compute SHA-256
  API->>PG: Lock admission, normalize course, check duplicate/limit
  API->>O: Store immutable source PDF
  API->>PG: Create and commit UPLOADED record
  API->>W: Non-blocking queue submission
  W->>PG: Claim lease + task token
  W->>O: Read source bytes
  W->>W: Extract text, sections, and overlapping chunks
  W->>Q: Store all chunk embeddings
  W->>G: Extract missing concept-graph batches
  G-->>W: Schema-constrained nodes + relationships
  W->>PG: Checkpoint each validated batch
  W->>G: Run one bounded cross-section linking pass
  W->>N: Store validated graph + provenance
  W->>PG: Mark GRAPH_BUILT then READY
```

Important behavior:

1. The API validates the extension, MIME type, configured 10 MiB limit, `%PDF-` signature, readability, page count, and password state.
2. It takes a PostgreSQL advisory transaction lock, normalizes the course name, checks the `(course, SHA-256)` duplicate, and enforces the installation limit.
3. The source object uses `courses/{course_uuid}/documents/{sha256}.pdf`; the durable database row is committed before queue submission.
4. A worker claims the row with a lease and task token. Every state transition must still match both values, preventing a stale worker from completing a newer retry.
5. PyMuPDF extracts the native text layer. Pages with fewer than the configured number of readable characters are rendered at a bounded DPI and passed to local Tesseract OCR. Native text is retained whenever OCR does not recover more content.
6. Native pages use block/line/span layout data to score headings from relative font size, boldness, length, numbering, capitalization, punctuation, and surrounding whitespace. Repeated running headers and footers are removed. OCR pages retain the text-only heading fallback because scanned images contain no PDF font metadata.
8. Graph extraction runs with a bounded request budget. Successful batches are preserved, validated, merged, and written to Neo4j.
9. `READY` is permitted only after the source still exists, all vectors are committed, graph construction has an explicit quality result, and counts are positive.

### 2. Graph construction for complex PDFs

The graph pipeline intentionally avoids a complicated agent system:

1. Group chunks by detected section or page fallback.
2. Sample the beginning, middle, and end of each section.
3. Select batches fairly across the document (`4` chunks per batch, up to `6` batches by default).
4. Ask for no more than `8` concepts and `10` relationships per batch.
5. Ask the configured provider for strict JSON Schema; optional same-provider JSON repair is disabled by default to protect quota.
6. Validate every node, endpoint, relationship type, and source-chunk reference locally.
7. Normalize concept identity using lowercase plus whitespace removal and deterministically merge batches.
8. Commit each validated batch to a PostgreSQL checkpoint, so later continuation attempts skip completed work.
9. Run one optional, bounded pass over representative excerpts from different successful sections to discover evidence-backed cross-section relationships.
10. Record local scanning, checkpoint, and graph-contribution counts separately so the UI can distinguish searchable chunks from partial graph coverage.

Allowed relationships are `PREREQUISITE_OF`, `PART_OF`, `EXPLAINS`, `RELATED_TO`, `CAUSES`, and `APPLIES_TO`.

Graph status is separate from document readiness:

- `GRAPH_READY`: at least two retained concepts and one valid relationship with acceptable coverage.
- `GRAPH_PARTIAL`: useful concepts exist, but graph density or section coverage is incomplete.
- `READY_WITHOUT_GRAPH`: chunks are searchable, but no valid concepts survived validation.

### 3. Question-answering flow

```mermaid
flowchart LR
  Question --> Ready[Resolve READY course documents]
  Ready --> Graph[Read scoped Neo4j subgraph]
  Graph --> Expand[Expand query with 1-hop and 2-hop prerequisites]
  Expand --> Vector[Qdrant top-k search]
  Vector --> Rerank[Cohere or local reranker]
  Rerank --> Evidence{Evidence threshold met?}
  Evidence -- No --> Refuse[Grounded refusal]
  Evidence -- Yes --> Sources[Build up to 5 citations]
  Sources --> Primary[Groq synthesis]
  Primary -. quota / timeout .-> Secondary[Gemini synthesis]
  Secondary -. unavailable .-> Passages[Return retrieved evidence]
```

Only READY upload IDs can contribute results. Retrieval uses deterministic, parameterized, read-only Cypher; it does not execute an LLM-generated database query. A matched concept is the anchor: inbound `PREREQUISITE_OF` paths are traversed to a maximum depth of two, direct and foundational prerequisites are kept separate, and both groups expand the semantic query. The traversal is course- and upload-scoped, cycle-safe, and bounded to five anchors. If no concept name matches, Qdrant receives the original question unchanged so a broad graph fallback cannot dilute vector retrieval. Qdrant then performs metadata-filtered semantic search, and reranking produces a provider-neutral score. Evidence confidence combines `70%` reranker probability with `30%` vector similarity. If nothing passes the minimum threshold, the API refuses before calling an LLM.

The dashboard distinguishes query anchors, direct prerequisites, and two-hop foundational prerequisites. The API also reports both hop counts in `graph_metadata.graph_expansion`; two-hop prerequisite edges use a dashed style so retrieval depth remains visible rather than being implied.

The answer prompt contains only bounded graph context and retrieved course passages. Citations expose readable PDF/page/section information and preview links, never internal vector IDs, storage keys, or database identifiers.

### 4. Exam generation

The exam service retrieves chunks only from the selected READY course, balances source passages across available documents, and asks for a strict JSON multiple-choice exam. Each question must have four options, an answer copied exactly from those options, an explanation, a topic, and at least one valid source ID. Questions with invented or missing citations are discarded. Groq-to-Gemini failover uses the same cooldown policy as graph extraction and answer synthesis.

### 5. Retry, crash recovery, and compensation

- Queue saturation leaves the PostgreSQL row in `UPLOADED`; it is deferred, not falsely failed.
- Workers heartbeat while synchronous parsing and provider calls run in threads.
- On restart, expired leases become auditable failed attempts and receive a new task token when retry budget remains.
- Failed executions and crash recovery use a three-attempt maximum; READY partial graphs can be continued for up to eight total attempts.
- Before every execution, old vectors for that upload are removed and deterministically rebuilt. Validated graph batches remain in PostgreSQL checkpoints.
- A graph continuation recomputes deterministic batch keys, skips matching checkpoints, and spends the current request budget only on missing batches.
- Before publishing the merged result, only the upload's old Neo4j projection is replaced, preserving an idempotent final graph.
- A failed execution removes partial vectors and graph entities before recording its safe failure category.

### 6. Authentication, public sample, and data sharing

This deployment uses one reviewer code, not individual accounts. The code is exchanged for a signed, expiring HttpOnly cookie; the frontend verifies that session with a second API request before unlocking protected controls. Everyone using the same reviewer session sees the same shared courses and uploads because there is no user or tenant column. This is intentional and is stated in the UI as **Shared portfolio demo**.

Unauthenticated visitors can only load the configured public sample and its source previews. They cannot upload, query the AI, generate exams, retry, or delete. Standard, login, public-sample, and expensive operations have separate process-local rate limits.

### 7. Deletion and automatic retention

READY or FAILED uploads can be deleted. Cleanup is ordered to avoid orphaned user data:

1. delete Qdrant points scoped to `upload_id`;
2. delete Neo4j concepts/relationships scoped to the upload and remove an orphaned course node;
3. delete the source object only when no retained record references the same content-addressed key;
4. delete PostgreSQL attempt/upload metadata and remove an empty course row.

Active documents cannot be deleted while a worker may own them. In protected-demo mode, the same deletion path automatically removes old reviewer uploads after the configured retention period; the public sample course is excluded.

## What changed from the distributed edition

- Removed Redis, the Celery app, worker service, broker URLs, and Celery task dispatch.
- Added a lifecycle-managed `asyncio` coordinator with a bounded queue and configurable concurrency (default `1`).
- Added PostgreSQL leases, heartbeats, task-token fencing, durable attempts, startup recovery, and a three-attempt retry cap.
- Preserved the durable stage contract and existing frontend polling/status response.
- Added content-addressed private object storage, duplicate admission control, PDF signature/parse checks, size/count limits, and partial-artifact cleanup.
- Added hosted embedding and reranking modes so the production API does not load Torch.
- Added liveness (`/api/v1/health`) and dependency-aware readiness (`/api/v1/ready`).
- Added strict public-deployment configuration validation and demo access-code protection.

## Durable processing contract

```text
UPLOADED -> EXTRACTING -> EXTRACTED -> CHUNKING -> CHUNKED
-> EMBEDDING -> EMBEDDED -> BUILDING_GRAPH -> GRAPH_BUILT -> READY
```

`READY` is committed only when the source object still exists, every chunk has been stored, graph construction has completed, provenance is present, the graph has an explicit quality status, and the document has a positive chunk count. Graph extraction runs in bounded section batches under a configurable free-tier request budget, retries strict-schema failures once with locally validated JSON, preserves successful batches, and deterministically merges concepts by normalized name. Provider quota exhaustion stops graph calls without discarding searchable vectors or completed graph sections. Incomplete coverage is reported as `GRAPH_PARTIAL`; zero validated concepts is reported as `READY_WITHOUT_GRAPH`. A failed execution removes vectors and graph nodes scoped to that upload/execution before it records `FAILED`.

Graph extraction samples the beginning, middle, and end of each detected PDF section. The application accepts only six relationship types, validates relationship endpoints, deduplicates lowercase whitespace-free concept names, and requires every retained concept to cite a real sampled chunk. Each accepted batch is checkpointed before processing continues, and one optional cross-section pass uses representative source excerpts to add only evidence-backed links. Neo4j concepts keep PDF, page, section, and upload provenance; clicking a dashboard concept opens its source page. At query time, Neo4j contributes separate bounded one-hop and two-hop prerequisite sets to semantic retrieval while retaining all valid relationship types for visualization. Groq is the primary LLM and Gemini is the preferred failover; Cerebras remains an optional legacy provider. A shared cooldown temporarily bypasses a provider after a quota response or timeout instead of repeating requests that are expected to fail.

Every worker-owned transition is fenced by both the current task token and lease owner. A stale task cannot advance or complete a newer attempt. The coordinator:

1. claims a dispatchable row with a PostgreSQL row lock;
2. records a lease owner and expiration;
3. extends the lease on the heartbeat interval;
4. runs the shared idempotent processor;
5. releases or clears the lease on completion, failure, or cancellation.

On startup and periodically, expired active rows are recovered. The interrupted attempt is retained as failed, a real new attempt/task token is created, and the document returns to `UPLOADED`. When the retry budget is exhausted it becomes terminally failed with an actionable message.

## Stack

- React 18, TypeScript, Vite, Tailwind CSS, Cytoscape.js
- FastAPI, Uvicorn, SQLAlchemy async, PyMuPDF
- PostgreSQL for durable state, attempts, leases, and canonical course identity
- Qdrant for vectors; Qdrant Cloud Inference in the low-memory profile
- Neo4j for document-provenance-scoped concepts and relationships
- S3-compatible private storage (Cloudflare R2 in production, MinIO locally)
- Groq as the primary provider, Gemini as the preferred failover, and optional legacy Cerebras support
- Cohere Rerank for the low-memory hosted profile

## Local development

Requirements: Python 3.12, Node.js, Docker, and at least one configured LLM provider. The deployed profile uses Groq as primary and Gemini as the preferred failover.

```bash
cp .env.example .env
docker compose up -d

python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements-local-models.txt
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

In a second terminal:

```bash
npm ci
npm run dev -- --host 127.0.0.1
```

Open `http://127.0.0.1:5173`. Local infrastructure includes PostgreSQL, Qdrant, Neo4j, and MinIO. There is no Redis container and no separate worker command.

Scanned-page fallback requires Tesseract on the API host. The provided API Docker image installs the English language pack automatically. For direct macOS development, install it with `brew install tesseract`; on Debian or Ubuntu, use `sudo apt-get install tesseract-ocr tesseract-ocr-eng`.

For the smaller hosted-provider environment, install `requirements.txt` instead and set:

```dotenv
EMBEDDING_PROVIDER=qdrant_cloud
EMBEDDING_MODEL_NAME=sentence-transformers/all-MiniLM-L6-v2
RERANK_PROVIDER=cohere
RERANK_MODEL_NAME=rerank-v3.5
COHERE_API_KEY=...
```

Gemini is the preferred failover and uses the existing lightweight HTTP dependency; no provider SDK is required.

## Configuration

Copy `.env.example`; it contains every supported setting. Important production settings are:

| Setting | Production value / purpose |
| --- | --- |
| `DATABASE_URL` | External managed PostgreSQL URL; TLS as required by the provider |
| `QDRANT_URL`, `QDRANT_API_KEY` | Qdrant Cloud cluster |
| `EMBEDDING_PROVIDER` | `qdrant_cloud` for a low-memory API |
| `EMBEDDING_MODEL_NAME` | `sentence-transformers/all-MiniLM-L6-v2` (384 dimensions) |
| `RERANK_PROVIDER`, `COHERE_API_KEY` | `cohere` and a secret API key |
| `NEO4J_*` | Neo4j Aura credentials |
| `S3_*` | Private R2 bucket endpoint and scoped credentials |
| `LLM_PROVIDER` | `groq` for the deployed primary/failover route; `gemini` and `cerebras` are supported alternatives |
| `GROQ_API_KEY` | Secret graph/synthesis provider key |
| `CEREBRAS_API_KEY` | Legacy optional fallback; ignored when `GEMINI_API_KEY` is configured |
| `CEREBRAS_MODEL` | Fallback model; default `gpt-oss-120b` |
| `CEREBRAS_BASE_URL` | OpenAI-compatible API base; default `https://api.cerebras.ai/v1` |
| `GEMINI_API_KEY` | Optional Gemini fallback key; never expose it to Vite or commit it |
| `GEMINI_MODEL` | Gemini fallback model; default `gemini-3.5-flash-lite` |
| `LLM_FAILOVER_COOLDOWN_SECONDS` | Minimum provider cooldown after quota/timeout; default `300`. Longer `Retry-After` headers are respected. State is process-local and resets on restart. |
| `GRAPH_BATCH_SIZE` | Chunks per graph request; default `4` |
| `GRAPH_MAX_BATCHES` | Maximum missing section batches attempted per processing run; default `6`. Validated batches are checkpointed and skipped by later continuations. |
| `GRAPH_GLOBAL_LINKING_ENABLED` | Default `true`. Runs at most one additional bounded extraction over representative successful sections to discover cross-section links. |
| `GRAPH_JSON_REPAIR_ENABLED` | Default `false`. Opting in permits one additional same-provider compatibility call after malformed strict output, increasing quota use. |
| `ALLOWED_ORIGINS` | Exact deployed frontend origin; comma-separated if necessary |
| `DEMO_ACCESS_TOKEN` | Secret reviewer code of at least 24 characters; configure it only in the hosting dashboard |
| `REQUIRE_UPLOAD_AUTH` | `true` for public deployments |
| `PUBLIC_SAMPLE_COURSE_ID` | Existing READY course exposed publicly as the read-only sample |
| `DEMO_UPLOAD_RETENTION_DAYS` | Delete reviewer uploads after this many days; default `3` |
| `DEMO_CLEANUP_INTERVAL_SECONDS` | Retention sweep interval; default `21600` (6 hours) |
| `STRICT_STARTUP_VALIDATION` | `true` for public deployments |
| `PROCESSING_CONCURRENCY` | `1` by default; bounded to `1..4` |
| `PROCESSING_QUEUE_CAPACITY` | In-memory admission buffer; durable overflow remains in PostgreSQL |
| `MAX_PDF_SIZE_MB` | Default `10` |
| `MAX_PDFS_PER_INSTALLATION` | Default `50` |
| `OCR_ENABLED` | Enable local Tesseract fallback for pages with sparse native text; default `true` |
| `OCR_LANGUAGE` | Installed Tesseract language pack; default `eng` |
| `OCR_DPI` | Sparse-page render resolution, bounded to `150..300`; default `200` |
| `OCR_MIN_NATIVE_CHARACTERS` | OCR pages below this alphanumeric-character threshold; default `40` |
| `OCR_MAX_PAGES_PER_DOCUMENT` | CPU-safety limit for OCR attempts in one PDF; default `50` |
| `OCR_PAGE_TIMEOUT_SECONDS` | Per-page Tesseract timeout; default `20` |

Cerebras is retained as an optional backup, while Gemini is now the recommended
free-tier backup. A valid API key does not guarantee generation access: a successful
model-list request does not prove that inference quota is available. HTTP 402
(`payment_required`) enters the quota cooldown path; no automatic purchase or
billing change is performed. If neither provider can generate, answers fall back
to retrieved evidence and graph processing preserves completed batches.

Generate the reviewer code with `openssl rand -base64 32` and save the result directly in the host's secret environment settings. Never paste the actual code into this README, `.env.example`, a commit, or a Vite variable. After login, the dashboard exchanges it for a signed, short-lived HttpOnly cookie and verifies that cookie with the API before enabling protected actions. Rate limiting is intentionally process-local because this deployment runs one API instance. Counters reset on restart and must be replaced by shared infrastructure before scaling horizontally.

The public route exposes only the configured READY sample course and its source-PDF previews. It cannot upload documents, run queries, or generate exams. Uploads made during authenticated reviewer sessions are automatically removed from PostgreSQL, Qdrant, Neo4j, and object storage after the retention window; the configured public sample course is excluded from cleanup. Retention runs only when `REQUIRE_UPLOAD_AUTH=true`.

Never commit `.env`, database URLs, provider keys, bucket credentials, or access tokens. The included example contains placeholders and local-only MinIO credentials.

## Deployment

The project is currently deployed from the separate portfolio repository: [frontend](https://conceptgraph-frontend.onrender.com/), [API health](https://conceptgraph-api.onrender.com/api/v1/health), and [API readiness](https://conceptgraph-api.onrender.com/api/v1/ready). `render.yaml` defines one free API service and one static frontend. It deliberately expects an external `DATABASE_URL` instead of creating Render's time-limited free PostgreSQL database. Add every `sync: false` secret in the Render dashboard; Git pushes trigger normal service builds after the Blueprint has been configured.

Current low-cost deployment:

- Static frontend: [Render Static Site](https://render.com/docs/static-sites).
- API: [Render Free web service](https://render.com/docs/free) while the measured hosted-provider profile fits; move to paid compute if real traffic or longer processing requires predictable uptime.
- PostgreSQL: an external free managed tier with persistence suitable for your portfolio window.
- Vectors/inference: [Qdrant Cloud free cluster](https://qdrant.tech/documentation/cloud/create-cluster/) with [Cloud Inference](https://qdrant.tech/documentation/cloud/inference/).
- Graph: one [Neo4j AuraDB](https://neo4j.com/docs/aura/getting-started/create-instance/) Free instance.
- PDFs: a private [Cloudflare R2](https://developers.cloudflare.com/r2/) bucket using its [S3-compatible API](https://developers.cloudflare.com/r2/api/), with public access disabled.
- LLM/reranking: [Groq](https://console.groq.com/docs/overview) primary, Gemini `generateContent` failover, and [Cohere Rerank](https://docs.cohere.com/v2/reference/rerank), each with account-side limits. Cerebras remains a legacy optional provider.

Current free tiers are not service-level guarantees. Render free web services spin down after inactivity and use an ephemeral filesystem; that is why source PDFs live in R2 and processing uses durable leases/recovery. Provider quotas, retention rules, and prices can change, so verify them before deployment. See [Render compute plans](https://render.com/docs/compute-plans) and [Cloudflare R2 pricing](https://developers.cloudflare.com/r2/pricing/) for current limits.

### Before deployment

1. Create PostgreSQL, Qdrant, Neo4j, R2, Groq, and Cohere credentials; create a Gemini API key for the recommended free-tier failover.
2. Create a private R2 bucket; disable public development URLs and use least-privilege object credentials.
3. Set all `sync: false` values in `render.yaml`, including the exact frontend `ALLOWED_ORIGINS` and API `VITE_API_BASE_URL`.
4. Use a new Qdrant collection when changing embedding model or dimension. Startup rejects incompatible existing vectors.
5. Run the verification commands below.
6. Deploy after the verification suite succeeds and the secret inventory has been checked.

The API image binds on port `8000`, runs as a non-root user, contains no local ML model artifacts, and stores no durable data on the container filesystem.

For Gemini failover, keep `LLM_PROVIDER=groq` and add `GEMINI_API_KEY` only to the API service environment. Never add it to the static frontend or any `VITE_*` variable. `GEMINI_MODEL` and the cooldown have safe defaults in both the application and Blueprint. Cerebras remains optional and is ignored when Gemini is configured.

## Schema changes and rollback

Startup runs additive, idempotent PostgreSQL DDL (`ADD COLUMN IF NOT EXISTS` and `CREATE INDEX IF NOT EXISTS`) before accepting traffic. Back up the database before the first production rollout.

Safe rollout:

1. snapshot/export PostgreSQL;
2. deploy the new API with processing concurrency `1`;
3. wait for `/api/v1/ready`;
4. upload one small PDF and observe it reach `READY`;
5. verify query, citations, concept graph, and exam generation.

Application rollback is safe because the migration is additive. Roll back the service image first; leave the added lease columns/indexes in place. Drop them only during a maintenance window after proving the older image does not depend on them. Do not delete Qdrant collections, graph data, PostgreSQL rows, or R2 objects as part of an application rollback.

Legacy local PDFs can be copied to object storage with a dry-run-first utility:

```bash
python scripts/migrate_pdfs_to_object_storage.py --dry-run
python scripts/migrate_pdfs_to_object_storage.py
```

If PostgreSQL claims a document is ready after a vector migration but its points are missing:

```bash
python scripts/reconcile_ready_vectors.py
python scripts/reconcile_ready_vectors.py --apply
```

## Capacity evidence

Measurements were taken on macOS with Python 3.12 using `resource.getrusage(...).ru_maxrss` after cached model loads:

| Probe | Measured peak resident memory |
| --- | ---: |
| Import API after removing eager model imports | ~193 MiB |
| Production container API import (Linux) | ~187 MiB |
| Local MiniLM embedding load + encode 8 strings | ~553 MiB |
| Local embedding + cross-encoder rerank | ~599 MiB |

The verified production image is about 140 MiB (147,140,460 bytes). The local model profile is not safe for a 512 MiB process. The Render blueprint uses hosted embedding/reranking and excludes Torch/SentenceTransformers from the production requirements. These are engineering measurements, not hosting guarantees; repeat them in the target container and use paid memory if real PDF processing approaches the limit.

The queue is intentionally conservative: one processor, eight buffered jobs, 10 MiB PDFs, and 50 PDFs per installation. Increasing concurrency multiplies parse buffers and provider traffic and should be backed by a new load/memory test.

## Small retrieval evaluation

The repository includes a deliberately small, human-annotated baseline rather than a large evaluation framework. [`evaluation/questions.json`](evaluation/questions.json) contains 15 questions for the READY `CYBER` course: 10 answerable questions with manually checked PDF pages and concepts, plus 5 out-of-scope questions that should be refused.

Measured on 30 August 2026 against the configured local pipeline and persistent portfolio stores:

| Metric | Result |
| --- | ---: |
| Correct document in top 5 | 8/10 |
| Correct source page in top 5 | 8/10 |
| Answers with inline citations | 6/10 |
| Unsupported questions refused | 5/5 |
| Average query time | 7.58s |

The complete per-question result is committed in [`evaluation/baseline-2026-08-30.json`](evaluation/baseline-2026-08-30.json). The latency includes a 25-second first-query model cold start; subsequent queries were faster. The baseline had zero request errors after graph context was bounded for the provider prompt. Two answerable questions were conservatively refused, and two otherwise grounded answers omitted an inline citation label. Those misses are kept visible instead of being edited out.

Run the same direct evaluation against the stores and providers configured in `.env`:

```bash
python scripts/run_evaluation.py \
  --direct \
  --output evaluation/baseline-local.json
```

To measure the deployed HTTP path, set `EVAL_API_BASE_URL` and keep `DEMO_ACCESS_TOKEN` in the environment or uncommitted `.env`; the runner never accepts or prints the reviewer code as a command-line argument:

```bash
EVAL_API_BASE_URL=https://your-api.example/api/v1 \
  python scripts/run_evaluation.py --output evaluation/baseline-hosted.json
```

This baseline measures only document/page retrieval in the top five citation sources, inline citation presence, evidence-based refusal, and end-to-end query latency. Expected concepts remain annotation notes and are not turned into a subjective answer-quality score.

### GraphRAG retrieval ablation

The evaluation runner can also compare three controlled retrieval variants over the same labelled questions:

- `vector_only` skips Neo4j completely and sends the original question to Qdrant;
- `one_hop` adds only direct prerequisite concept names;
- `two_hop` adds direct and foundational prerequisite concept names.

All three variants reuse the same READY-document scope, Qdrant collection, result count, reranker, and evidence threshold. Answer synthesis is intentionally skipped so LLM wording, quota, and generation latency do not obscure whether graph expansion improved retrieval.

```bash
python scripts/run_evaluation.py \
  --direct \
  --ablation \
  --output evaluation/ablation-local.json
```

The JSON report contains per-question sources, graph-anchor/expansion counts, per-mode summaries, and deltas from the vector-only baseline. The console prints a compact comparison of top-five document/page hits, supported evidence coverage, unsupported-question refusals, average latency, and p95 latency.

For a repeatable local multi-document run that avoids PostgreSQL, use the isolated fixture runner:

```bash
python scripts/run_fixture_ablation.py --reference-graph --repeats 3 \
  --output evaluation/ablation-reference.json
```

The reference graph is intentionally authored and labelled as such. A normal provider-backed attempt writes [`evaluation/fixture-graphs.json`](evaluation/fixture-graphs.json). Neo4j connectivity failure produces a [`status: blocked`](evaluation/ablation-provider-limited.md) report. If extraction produces no traversable graph, the runner records `incomplete_no_graph` and exits unsuccessfully: the resulting vector scores do not establish graph improvement.

The [provider-generated run on 6 September 2026](evaluation/ablation-provider-2026-09-06.md) completed 66 retrieval requests (22 questions × 3 modes), with zero request errors. Extraction produced 8 nodes and 7 edges for the web PDF, then provider quota limits left the other two graphs empty. All modes found every required source after reranking for 17/17 supported questions; all retained complete evidence after the evidence gate for 13/17 and refused 5/5 unsupported questions. Average retrieval latency was 0.05s / 0.32s / 0.28s for vector / one hop / two hops. No two-hop expansion terms were used in that run, so it does not establish the benefit of deeper traversal. The latest provider-backed report, when available, is committed as [`evaluation/ablation-provider-current.md`](evaluation/ablation-provider-current.md); a blocked or partial report remains evidence of the actual dependency/quota state rather than being relabelled as a successful comparison.

The [current provider-generated run on 9 September 2026](evaluation/ablation-provider-current.md) produced 61 validated concepts and 44 relationships across all three PDFs, then completed all 66 retrieval requests without errors or graph fallbacks. Graph expansion improved raw all-required-source recall from 16/17 to 17/17 for both graph modes, but the evidence gate remained 12/17 in every mode. Average retrieval latency was 0.06s / 0.48s / 0.41s for vector / one hop / two hops, and three questions used two-hop terms. The result supports a narrow claim: graph expansion helped one raw multi-source retrieval case in this fixture, while adding latency and not improving the final evidence-gated outcome.

Interpret the result conservatively: one-hop or two-hop is useful only when it improves labelled retrieval enough to justify its latency. Equal scores are also meaningful—they show that the graph adds explainability for those questions but not retrieval accuracy. This small single-course set is portfolio evidence, not a claim of statistical significance.

#### Multi-document fixture result

[`evaluation/questions-multidoc.json`](evaluation/questions-multidoc.json) expands the fixture to 22 questions across three authored PDFs (17 answerable and 5 intentionally unsupported). [`evaluation/ablation-reference.md`](evaluation/ablation-reference.md) is an actual three-repeat run using the PDF parser, local MiniLM embeddings, in-memory Qdrant, a temporary Neo4j graph, the cross-encoder, and the evidence gate. It uses an explicitly authored prerequisite chain so retrieval depth can be measured independently from LLM extraction quality; it is not presented as an LLM-quality score.

| Metric (51 answerable runs: 17 questions × 3 repeats) | Vector only | One hop | Two hop |
| --- | ---: | ---: | ---: |
| Primary expected page in top 5 | 45/51 | 45/51 | 45/51 |
| All required source pages in raw top 5 | 48/51 | 51/51 | 51/51 |
| All required source pages after reranking | 51/51 | 51/51 | 51/51 |
| All required sources after evidence gate | 39/51 | 39/51 | 39/51 |
| Unsupported questions refused | 15/15 | 15/15 | 15/15 |
| Average retrieval time | 0.05s | 0.26s | 0.28s |

The graph improved raw multi-source recall on this small fixture (48→51) but did not improve the final evidence-gated score, and added about 0.2 seconds per query. Repeated runs are stability measurements, not 51 independent questions. The provider-backed extraction attempt is preserved in [`evaluation/fixture-graphs.json`](evaluation/fixture-graphs.json); when the provider quota/network is unavailable, the app marks the graph partial and query retrieval falls back to vector search.

## Repository map

| Path | Purpose |
| --- | --- |
| `src/pages/Dashboard.tsx` | Main protected workspace, upload queue, answers, citations, graph status, deletion/retry controls |
| `src/components/ConceptGraphCanvas.tsx` | Cytoscape graph rendering and source-node interaction |
| `src/components/DemoAccessGate.tsx` | Reviewer-code exchange and server-verified session gate |
| `src/components/PublicSampleCourse.tsx` | Read-only public graph and PDF source preview |
| `src/components/SavedSampleCourse.tsx` | Static sample answers, citations, source-page links, and illustrated graph for quota-free demos |
| `app/api/endpoints/` | FastAPI auth, upload/status, public sample, query, and exam boundaries |
| `app/core/processing_coordinator.py` | Bounded queue, dispatcher, lease claims, worker lifecycle, restart recovery |
| `app/services/document_processing_service.py` | Durable stage orchestration and failure compensation |
| `app/services/parser_service.py` | PyMuPDF text extraction, heading detection, and overlapping chunks |
| `app/services/ingestion_service.py` | Qdrant writes, graph batching/validation/merge, Neo4j provenance, cleanup |
| `app/services/rag_service.py` | READY-scoped graph and vector retrieval |
| `app/services/rerank_service.py` | Local/Cohere provider-neutral reranking |
| `app/services/synthesis_service.py` | Evidence-bounded answers and provider degradation |
| `app/services/gemini_service.py` | Lightweight Gemini REST client and normalized provider errors |
| `app/services/cerebras_service.py` | OpenAI-compatible Cerebras client and normalized provider errors |
| `app/services/provider_failover.py` | Thread-safe provider cooldown circuit breaker |
| `app/services/exam_service.py` | Citation-validated course MCQ generation |
| `app/services/upload_service.py` | Durable records, attempts, leases, retries, completion gates, deletion metadata |
| `app/models/document_upload.py` | PostgreSQL course/upload/attempt records and resumable graph-batch checkpoints |
| `ENGINEERING_HANDBOOK.md` | Runtime invariants and safe-change rules |
| `evaluation/` and `scripts/run_evaluation.py` | Human-labelled dataset, baseline, and repeatable evaluation runner |
| `render.yaml` | API/static-site Blueprint and secret placeholders |

## Known limitations and honest roadmap

These are intentional portfolio boundaries, not hidden claims:

- **OCR is text-focused.** Sparse and image-only pages use local Tesseract with page provenance and confidence reporting. Layout-aware font scoring applies to native PDF text; OCR pages use textual heading heuristics. Complex tables, diagrams, handwriting, and equation-to-LaTeX conversion remain outside the current scope.
- **Shared data model.** One reviewer code grants access to one shared workspace. Real multi-user support needs user identities, tenant ownership on every PostgreSQL row, Qdrant payload, Neo4j entity, and object key, plus authorization checks on every query and cleanup path.
- **Process-local rate limits and circuit breaker.** They match the single Render API instance. Horizontal scaling needs Redis or another shared coordination store.
- **Single bounded worker by default.** This protects memory and free provider quotas but limits throughput. Scale only after measuring parse memory, provider budgets, and database connection pools.
- **Small evaluation sets.** The original 15-question course baseline and 22-question multi-document ablation demonstrate measurement, not general academic QA quality. Expand across subjects, layouts, scanned PDFs, and adversarial unsupported questions.
- **Graph entity resolution is deliberately simple.** Lowercase/whitespace normalization is explainable but will not merge synonyms or disambiguate homonyms. More advanced entity resolution belongs after a labelled graph-quality benchmark exists.
- **Free-tier cold starts and quotas.** The demo can be slow after inactivity and no free provider offers a production SLA. Durable recovery and failover reduce impact but cannot create capacity that every provider has exhausted.
- **No collaborative isolation or audit identity.** Attempts are auditable at the processing level, but actions are not attributed to individual people.

Recommended next phases, in order:

1. Expand the labelled evaluation dataset and run it in CI against deterministic fixtures, including scanned-page cases.
2. Add provider metrics: selected provider, fallback count, latency, quota errors, and per-operation token use without logging prompts or keys.
3. Add optional table reconstruction only after measuring OCR and layout quality on representative academic PDFs.
4. Add real authentication and tenant-scoped storage only if the project becomes a shared product.
5. Move workers/rate limits to shared infrastructure only when traffic justifies the extra operational complexity.

## Engineering notes

### Design decisions

- **PostgreSQL is the source of truth.** The queue can disappear without losing the job.
- **Derived data is provenance-scoped.** Qdrant and Neo4j artifacts can be deleted or rebuilt by upload.
- **The graph is optional enrichment.** An empty graph does not destroy useful vector retrieval.
- **LLM output is untrusted input.** Schema, relationship, endpoint, size, source, and quality checks run before persistence.
- **Reliability is proportional to the project.** A bounded in-process coordinator is simpler and cheaper than Redis/Celery while leases preserve restart safety.
- **Security claims match the deployment.** It is called a shared portfolio demo, not incomplete multi-user authentication.
- **Results are measured honestly.** The baseline includes misses and refusals instead of manually improving the reported numbers.

## Verification

Latest local verification: **161 backend tests passing** (`python -m unittest`), production frontend build passing, and Python compilation passing. CI configuration is included for repeatable checks; hosted results depend on the configured services and secrets.

```bash
source .venv/bin/activate
python -m compileall -q app tests
python -m unittest discover -s tests -v
npm ci
npm run build
docker compose config -q
```

Tests focus on system boundaries and failure behavior: durable stage transitions, leases, fencing, expired-attempt recovery, bounded queue behavior, deferred admission, retry exhaustion, idempotent cleanup, READY deletion, demo retention, empty graphs, resumable graph checkpoints, cross-section linking, relationship validation, provider timeouts/rate limits, Gemini/Cerebras request compatibility and circuit-breaker failover, scanned PDFs without text, READY gating, graph sampling and provenance, hosted inference/reranking, object storage, PDF byte ranges and citation links, verified auth sessions, public sample isolation, evidence refusal, and readiness-sensitive course behavior.

## API surface

- `GET /api/v1/health` — PostgreSQL liveness
- `GET /api/v1/ready` — PostgreSQL, Qdrant, Neo4j, object storage, and coordinator readiness
- `POST|GET|DELETE /api/v1/auth/session`
- `GET /api/v1/public/sample` — rate-limited read-only sample graph
- `GET /api/v1/public/sample/uploads/{upload_id}/preview` — sample-only PDF preview
- `POST /api/v1/ingest/upload`
- `GET /api/v1/ingest/status/{task_id}`
- `GET /api/v1/ingest/uploads`
- `GET /api/v1/ingest/courses`
- `POST /api/v1/ingest/uploads/{upload_id}/retry`
- `GET /api/v1/ingest/uploads/{upload_id}/preview`
- `DELETE /api/v1/ingest/uploads/{upload_id}` (READY or FAILED documents only)
- `POST /api/v1/query`
- `POST /api/v1/exam/generate`

The frontend also exposes `/sample`, a fully static recruiter path with prepared answers, citations, and an illustrated prerequisite graph. The protected dashboard adds a retrieval-mode selector (`vector_only`, `one_hop`, or `two_hop`) so reviewers can compare the same question across strategies. If Neo4j is unavailable or times out during a graph-mode query, the API keeps the request useful by returning vector evidence and records the fallback in `graph_metadata`.

## Repository safety

This portfolio edition was developed from the [original ConceptGraph repository](https://github.com/Ninjax26/conceptgraph). The separate [portfolio repository](https://github.com/Ninjax26/conceptgraph-portfolio) is configured as `origin`. The source repository remains `upstream` for fetches only and its push URL is disabled, preventing portfolio changes from being pushed back to the original repository accidentally.
