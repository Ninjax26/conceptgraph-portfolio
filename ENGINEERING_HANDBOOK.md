# ConceptGraph Engineering Handbook

This document describes the portfolio edition's runtime invariants. Setup, deployment inputs, capacity evidence, and operator commands live in `README.md`.

## System boundaries

The React SPA calls one FastAPI service. FastAPI owns both HTTP handling and a bounded background coordinator. PostgreSQL is the only authoritative state store. Qdrant vectors and Neo4j graph entities are derived, provenance-scoped projections. S3-compatible object storage holds immutable PDF sources.

External AI services are deliberately provider-configurable:

- graph extraction, synthesis, and exams: Groq by default, with Gemini failover and optional legacy Cerebras support;
- embeddings: local MiniLM or Qdrant Cloud Inference;
- reranking: local cross-encoder or Cohere;
- Gemini uses a lightweight REST adapter and is also supported as a direct provider.

The production requirements use hosted embeddings/reranking so the API process does not load Torch. Local models are an explicit optional install.

## Authoritative records

`courses` stores canonical course identity. Normalized names collapse case and whitespace; display names remain user-readable.

`document_uploads` stores the current logical state of each PDF, including:

- immutable upload ID and content hash;
- current task token and processing attempt count;
- current durable stage and safe failure classification;
- lease owner, expiration, and last heartbeat;
- source object key and derived-store counts;
- validated graph quality status (`GRAPH_READY`, `GRAPH_PARTIAL`, or `READY_WITHOUT_GRAPH`);
- completion and update timestamps.

`processing_attempts` is append-only audit history for each execution. A recovered execution receives a new row and task token rather than mutating history to look successful.

## Admission and deduplication

Upload validation enforces extension, media type, configurable byte limit, `%PDF-` signature, PyMuPDF readability, non-empty pages, and no password protection. Admission takes a PostgreSQL advisory transaction lock, canonicalizes the course, checks the `(course, SHA-256)` duplicate, and enforces the installation-wide PDF count before creating a new record.

The source is written to:

```text
courses/{course_uuid}/documents/{sha256}.pdf
```

The endpoint commits the durable row before submitting to the in-memory queue. Queue saturation is not an upload failure; the durable `UPLOADED` row remains dispatchable.

## Coordinator lifecycle

The coordinator starts inside FastAPI's lifespan after schema initialization and Qdrant dimension validation. It owns:

- one bounded `asyncio.Queue`;
- `PROCESSING_CONCURRENCY` worker loops;
- one periodic dispatcher/recovery loop;
- a process-unique lease owner prefix.

Submission is non-blocking. Duplicate in-memory items are suppressed locally. Multiple API processes may still hold the same candidate in memory, but PostgreSQL lease acquisition with row locking permits only one owner to execute it.

Shutdown cancels dispatch first, then workers. Processor cancellation releases the current lease so a replacement instance can recover immediately. If the process is killed before release, expiration provides the fallback.

## Leases, heartbeats, and fencing

A worker claims a row only when it is dispatchable and has no unexpired foreign lease. Claiming records the owner and expiration atomically. The configured lease must be more than twice the heartbeat interval.

Every processor transition supplies:

- upload ID;
- current task token;
- lease owner.

The update is rejected unless all three still match the active record. This prevents delayed or superseded work from completing a newer attempt.

The heartbeat loop extends both document/attempt heartbeat metadata and the lease. Long synchronous parsing or provider SDK calls run through `asyncio.to_thread`, keeping the event loop available for heartbeats and HTTP requests.

## Stage invariants

The only successful order is:

```text
UPLOADED
EXTRACTING
EXTRACTED
CHUNKING
CHUNKED
EMBEDDING
EMBEDDED
BUILDING_GRAPH
GRAPH_BUILT
READY
```

At least one page and one chunk are mandatory. Every chunk must be accepted by the vector write. `READY` additionally requires:

- current stage is `GRAPH_BUILT`;
- non-empty canonical course UUID;
- non-empty source storage key;
- positive committed chunk count;
- one validated graph quality status, including an explicit no-graph outcome;
- source object still exists.

`mark_completed` enforces these rules again at the persistence boundary.

## Graph quality and provenance

Graph extraction is a bounded section-batch workflow rather than a multi-agent system. Chunks are grouped by detected section, each section contributes beginning/middle/end evidence, and a fair selector distributes the configured request budget across the document. Production defaults allow four chunks per request and at most six missing section batches per processing run. Each batch asks for at most eight concepts and ten relationships and supplies stable source chunk IDs.

Provider output is schema-validated, limited to six relationship types (`PREREQUISITE_OF`, `PART_OF`, `EXPLAINS`, `RELATED_TO`, `CAUSES`, and `APPLIES_TO`), checked for valid endpoints, deduplicated using lowercase whitespace-free concept names, and rejected at node level when it cites an unknown source chunk. Both arrays must be explicitly present: `{}` is invalid, while `{"nodes": [], "relationships": []}` is a processed empty result. Provider parsing rejects unsupported relationship types and checks raw array sizes before deduplication. A non-empty output with no verifiable source is a failure, not an empty success.

Strict JSON Schema is attempted first. `GRAPH_JSON_REPAIR_ENABLED` defaults to true and permits at most **one additional repair request per batch across the configured provider chain**, after ordinary failover has failed with a structural error. The repair regenerates a small graph from the bounded source excerpts and never includes raw provider error text or previous output. Quota, timeout, configuration, and general request errors do not trigger repair. Set the flag to false to avoid the extra request. The graph output cap is 2,000 tokens; explicit Groq/Gemini truncation signals are reported separately. Gemini thought parts are excluded from final-text parsing. Each actual generation, including repair, receives its own sanitized provider metric.

Each successful batch, including an explicit empty result, is committed to PostgreSQL under a deterministic hash of its section, chunk IDs, and source text. A continuation loads matching checkpoints and selects only missing batches, so quota exhaustion or a restart does not repeat accepted LLM work. Legacy empty checkpoints without an explicit empty-result marker are revalidated once.

Provider contexts are capped at 5,000 characters for the initial structured request and 3,000 for repair. The allocator splits on source headers, not paragraph breaks, and includes separators and the linking preamble in its budget. Long display headers can be shortened while preserving the exact source ID and text evidence. A single long unstructured line is also bounded. This prevents paragraph-heavy PDFs from bypassing the context limit.

After section extraction, one optional global pass samples one representative source excerpt from up to eight successful sections. It uses the same schema, endpoint, relationship, source-ID, and size validation as local batches. Version-four output is checkpointed separately, even when it returns no concepts or adds no relationships. `global_linking_processed` records a valid completed pass; `global_linking_succeeded` is true only when the merge adds relationships. Failure of this enrichment pass never invalidates the section graph.

Quality is reported separately from document readiness. `GRAPH_READY` requires at least two concepts, at least `ceil(node_count / 3)` relationships, and no unresolved or budget-deferred section batches. A non-empty graph below that floor is `GRAPH_PARTIAL`; zero retained concepts is `READY_WITHOUT_GRAPH`. A fully processed section that returned no concepts is counted separately and does not by itself invalidate a usable graph. The dashboard distinguishes processed sections, sections that contributed concepts, and processed empty sections. All three document outcomes remain vector-searchable.

Authenticated upload result metadata includes safe section/batch categories for malformed JSON, invalid schema/endpoints, duplicate IDs, unsupported relationship types, unknown sources, size limits, truncated output, quota, unavailable providers, configuration/request failures, and budget deferrals. Partial source filtering records discarded-node/relationship counts and survives checkpoint reuse. Raw completion bodies and exception text are never copied into diagnostics or provider metrics. These categories diagnose extraction mechanics; they do not establish factual accuracy of the retained concepts.

Each retained Neo4j concept stores upload ID, PDF filename, source chunk ID, page number, and detected section heading. Graph retrieval returns those properties to the dashboard, where a selected concept can open the original PDF at its source page.

Text extraction first uses PyMuPDF's native block/line/span dictionary with text-only flags. A document-wide character-weighted median estimates body font size. Native lines receive a deterministic heading score from relative size, boldness, length, numbering, capitalization, punctuation, and vertical gaps; a score of four is required. Repeated text in the top or bottom page margins is filtered when it appears on at least 30% of pages, preventing running headers and footers from becoming sections.

A page triggers OCR when native text is sparse or shows corruption (replacement/control characters, repeated letter runs, or unusually dense symbols). The deterministic text score is a corruption heuristic, not a spelling checker or accuracy probability. OCR replaces native text only when its text quality and configured engine-confidence threshold pass and it improves quality or recovers more text from a sparse layer. A longer garbled result cannot overwrite readable native text. Native text is retained with a warning when optional OCR fails; pages with no reliable extraction are reported as unreadable and contribute no chunks.

Readable mixed pages can additionally OCR large image regions without sufficient native-text coverage. Native layout lines remain available for heading detection; new image text is deduplicated and chunked separately using text heuristics. Small logos and image regions already covered by a substantial native text layer are skipped. This is text recovery inside images, not interpretation of charts or engineering geometry.

Tesseract's first pass uses automatic orientation/script detection (`--psm 1`, with `osd` data explicitly installed in the API image). A poor result can use one alternate segmentation pass at up to 300 DPI. Both subprocess calls share a page deadline. Rendering is grayscale, capped by `OCR_MAX_RENDER_PIXELS` and a maximum dimension; downsampling is reported for review. Required scanned-page work stops at the configured page limit; optional mixed-page work can be skipped while preserving native text.

Chunks retain extraction method, engine confidence, text quality, heading method/score, and warning codes. Completed metadata separates native, OCR, hybrid, and unreadable pages, counts OCR attempts, and lists pages requiring review. TSV parsing rejects non-finite/out-of-range confidence values. OCR timeouts are retryable timeout failures rather than missing-engine errors. Tesseract confidence is not calibrated word accuracy. Japanese recognition needs separately installed language data and evaluation; tables, diagrams, handwriting, and equation-to-LaTeX conversion remain outside the supported contract.

## LLM resilience and failover

`LLM_PROVIDER=groq` keeps Groq as the primary provider. When `GEMINI_API_KEY` is configured, graph extraction, answer synthesis, and exam generation fail over to Gemini after a Groq quota response, timeout, connection failure, provider-side server error, or invalid structured graph. Cerebras remains a legacy optional backup and is ignored when Gemini is configured.

A process-wide, thread-safe circuit breaker records a provider cooldown, defaulting to 300 seconds. Requests bypass a cooling provider instead of repeatedly spending latency and quota on a failure that is expected to recur. Provider SDK retries are disabled on the Groq calls covered by failover so routing happens promptly.

Gemini uses a small HTTP adapter around the official `generateContent` endpoint; the key is sent in the `x-goog-api-key` header and never included in a URL or public error. Cerebras uses its OpenAI-compatible chat-completions endpoint when selected. Prompt limits and local validation enforce the application's bounded graph contract. Provider response bodies and credentials are never copied into public errors.

If both providers are unavailable:

- graph processing preserves completed batches and may finish as `GRAPH_PARTIAL` or `READY_WITHOUT_GRAPH` because vector search remains useful;
- answer synthesis returns retrieved, citation-labelled evidence instead of inventing an answer;
- exam generation returns a safe temporary-unavailability response.

## Idempotency and compensation

Each execution has an execution token. Before processing starts, Qdrant points for the upload are removed and deterministically rebuilt. Validated LLM batches remain in PostgreSQL checkpoints. Immediately before publishing a merged graph, only the prior Neo4j projection for that upload is removed and replaced. New chunk payloads, graph nodes, and relationships carry upload provenance and the execution token.

Qdrant point IDs and graph concept IDs are deterministic within their document/course scope. Re-execution therefore replaces or merges known artifacts instead of accumulating anonymous duplicates.

Any processor failure:

1. classifies the error into a safe public category;
2. removes partial vectors and graph nodes for the upload;
3. records attempt/document failure if fencing still permits it;
4. clears the lease.

READY and FAILED document deletion repeats provenance-scoped Qdrant and Neo4j cleanup before removing PostgreSQL metadata. It also removes an empty Neo4j course node after its final concepts are deleted. The source object is deleted only if no other retained record references its content-addressed key, and a PostgreSQL course row is removed only after its final document is deleted. Active documents remain protected from deletion while a worker can still own their lease.

## Recovery and retries

The dispatcher periodically examines active rows whose leases are absent/expired. For each interrupted record it:

1. marks the old attempt failed with a worker interruption reason;
2. checks the three-attempt crash/failure recovery cap;
3. creates a new attempt and task token when budget remains;
4. resets the current stage to `UPLOADED` and clears lease metadata;
5. queues the durable candidate when memory capacity is available.

Manual retry of a failed execution uses the same three-attempt cap and creates a new task token. A READY `GRAPH_PARTIAL` or `READY_WITHOUT_GRAPH` document can instead continue checkpointed graph extraction for up to eight total attempts. Retry cannot proceed for a permanent document/configuration failure or a missing source object. An old status URL remains useful because task lookup can resolve attempt history to the document's current attempt.

## Retrieval and evidence

Query/exam flows first resolve canonical course context from READY documents only. Failed and active uploads cannot contribute vectors, graph metrics, questions, or answers.

Graph retrieval uses parameterized, read-only Cypher scoped to canonical course IDs and READY document IDs. Native driver records preserve relationship direction. Each matched concept is an anchor for an inbound `PREREQUISITE_OF` traversal with a hard maximum of two hops. Direct and foundational prerequisite names are deduplicated separately before they expand the semantic query; all valid typed relationships remain visible in the concept map. Breadth is bounded by five anchors and depth is bounded by two, preventing an uncontrolled whole-graph expansion.

When no term-matched anchor exists, the service may return a bounded course graph for visualization, but it does not use that broad graph to expand the semantic query. Qdrant receives the original question unchanged. This keeps graph absence or vocabulary mismatch from reducing the vector-search baseline.

The production retrieval path deliberately uses a deterministic Cypher template rather than executing LLM-generated database queries. A legacy provider-backed Cypher helper remains isolated from the request path, but every executed query is validated as read-only and parameterized. This reduces injection risk and makes course/document scoping explainable.

Qdrant search filters by READY upload IDs. Its query text contains the original question plus separately labelled direct and foundational prerequisite terms only when an anchor was found. Hosted and local embeddings use the same 384-dimensional MiniLM space and normalized cosine collection contract. Startup rejects an incompatible collection and instructs the operator to choose a new collection name.

`RETRIEVAL_CANDIDATE_LIMIT` bounds the reranking pool (default 10, maximum 100). New vectors also store a bounded `identifier_terms` keyword payload for alphanumeric IDs, versions, and longer numeric tokens. Recognized identifiers are case-folded and normalized across whitespace, underscore, and common hyphen variants; dots in versions are preserved. When the original question contains identifiers, `RETRIEVAL_IDENTIFIER_LOOKUP` permits one additional payload-only query, limited by `RETRIEVAL_IDENTIFIER_LIMIT`, with the same READY upload filter. Named identifiers take priority over scenario quantities. It requires no second embedding or LLM request. Reciprocal rank fusion merges/deduplicates the candidate lists and retains the configured total limit. Fusion scores are ranking aids, never cosine scores; payload-only candidates have no vector score and receive a conservative zero vector contribution at the evidence gate. An optional lookup failure is surfaced in candidate metadata while dense retrieval remains usable.

The `upload_id` and `identifier_terms` payloads are indexed on ingestion. Legacy vectors without identifier metadata remain searchable through dense retrieval; they need reprocessing to gain the new identifier index data. No live vectors are backfilled by the application or the scale benchmark. An empty READY scope returns no candidates before embedding.

Reranking returns a provider-neutral logit. Cohere probabilities are converted to logits so the existing sigmoid-based evidence gate behaves identically. Low evidence returns a grounded fallback without asking the synthesis LLM to invent an answer. User-facing sources expose document, page, section, and supporting passage but not internal vector IDs or file keys.

Cohere indices and relevance scores must have valid types, ranges, and finite values. Local non-finite scores are rejected; invalid evidence scores cannot become confident citations. With `RETRIEVAL_VERIFY_IDENTIFIERS`, evidence must include every recognized named identifier requested by the question, allowing case and simple separator variation but not another identifier's prefix or suffix. Bare scenario quantities are not required verbatim: a question may supply a hypothetical value for reasoning. Verification is repeated against the actual cited excerpts, and questions exceeding the eight-identifier verification budget are refused explicitly. This heuristic reduces identifier substitution; it does not establish factual correctness or resolve semantic aliases. It can be disabled for a controlled comparison.

Citation excerpts remain bounded at 900 characters but are selected around question terms and matching identifiers across the chunk, rather than always taking its prefix. Reranking uses the same literal-window selector with a 1,600-character limit, exposing relevant tail content to local model token limits and bounding hosted rerank input. Stored chunk text is not mutated. These windows do not guarantee that every contextual qualification is preserved. Deduplication compares the selected excerpt so passages sharing boilerplate can retain distinct evidence. The HTTP query and evaluation runners use `prepare_evidence` to keep evidence selection consistent. Query metadata includes candidate counts and measured course-resolution, retrieval, reranking, evidence-selection, and generation times; a graph-fallback timer excludes the subsequent vector call.

### Retrieval ablation contract

`RetrievalService` exposes three explicit modes while keeping `two_hop` as the product default. `vector_only` never opens a Neo4j session and supplies no graph terms. `one_hop` executes a `PREREQUISITE_OF*1..1` traversal. `two_hop` executes the bounded `PREREQUISITE_OF*1..2` traversal described above. Each result records its mode, maximum depth, anchor-match flag, and expansion counts in graph metadata.

Graph enrichment is optional at query time. A Neo4j exception or five-second timeout is caught at the retrieval boundary, the original question is sent through the same READY-scoped vector path, and metadata records `requested_mode`, `retrieval_mode: vector_only`, and `fallback_reason: graph_unavailable`. Vector-store failures are not hidden by this fallback. The dashboard surfaces the degraded mode so a reviewer can distinguish a graph answer from a safe vector answer.

The ablation runner compares these modes without answer synthesis. This holds document scope, vector index, top-k, reranking, and evidence thresholds constant, making graph depth the intended independent variable. It records top-five document/page hits, evidence availability, unsupported-question refusal, average/p95 retrieval latency, and errors. Deltas are calculated against `vector_only`; no graph mode is declared better unless the labelled metrics support it.

Stage metrics now include MRR@5, source recall, and page-label precision@5 before reranking, after reranking, and after evidence selection. Page-label precision is a relevance proxy, not verified passage or answer correctness. Candidate-pool coverage distinguishes a true retrieval miss from a reranking cutoff; supported refusals and unexpected evidence on unsupported questions are counted separately. Failed requests do not inflate stage scores or count as successful refusals. Multi-source cases without a primary-page label are excluded from that primary-page denominator while still contributing to all-required-source metrics.

`scripts/evaluate_retrieval_scaling.py` exercises 10/50/100 authored PDF corpora with cached local models and isolated in-memory Qdrant. It compares bounded dense pools and identifier enrichment/verification, measures stage latency, and separates development, diagnostic validation, and fresh authored held-out questions. Thresholds remain unchanged: higher candidate counts are not automatically better, and a local cross-encoder experiment does not calibrate hosted Cohere scores. The benchmark makes no LLM calls and writes no production data. It measures warm sequential retrieval using Qdrant local brute-force search, not hosted ANN, concurrent users, upload admission limits, or distributed processing capacity.

## Security model

This is a shared-secret portfolio demo, not multi-user authentication. Public deployments require an access token of at least 24 characters. Login compares secrets in constant time and issues a signed, expiring, HttpOnly cookie. Secure cookies and exact CORS origins are required in production.

The frontend does not trust the login response alone. It performs a separate session-status request and unlocks protected controls only when the API validates the signed cookie. The reviewer code exists only in deployment secrets and is never compiled into the SPA or published in documentation.

Cookie-authenticated writes require `X-ConceptGraph-Request: 1`, forcing cross-origin browser writes through CORS preflight. Explicit bearer authentication remains supported for scripts. Strict production configuration allows only the configured frontend origins, protected responses use `Cache-Control: no-store`, and the UI locks on a protected API 401. The process-local limiter expires prior windows and caps tracked keys to bound memory.

Unauthenticated visitors receive one configured, pre-uploaded READY course through a dedicated read-only endpoint. That endpoint returns the bounded concept graph and permits PDF previews only for documents already belonging to that course; it cannot invoke LLM, upload, query, exam, retry, or deletion operations. Public sample reads have their own IP-fingerprinted process-local rate limit.

The frontend `/sample` route is additionally backed by committed, generated fixture PDFs and editorial answer cards. It remains useful when the API is sleeping, databases are unavailable, or provider quota is exhausted; its graph is explicitly labelled as an illustrated reference, not a live LLM extraction.

The saved sample selector preserves the original Computing Foundations course and adds Equipment Documentation (`?course=technical`). Both fetch same-origin static JSON/PDF assets; switching cancels the old request and clears the prior answer/preview. The equipment corpus has a specification, maintenance guide, and inspection record. Its first inspection page is rasterized with no hidden text layer. Page and paragraph citations point into the authored fixture; displayed passages are reference text, not dynamically extracted browser OCR.

Equipment graph nodes and relationships are explicit editorial data with source references, rather than treating a list of concepts as an inferred prerequisite chain. `PART_OF` and `APPLIES_TO` illustrate supported document connections without inferring a fault diagnosis. The motor-power refusal intentionally draws no answer graph. Revision labels describe the supplied documents; neither the selector nor the saved answers implement live revision resolution or semantic conflict detection.

The sample keeps graph element props stable when opening a citation. Its small authored graphs request a breadth-first layout so their labels remain legible; reviewer graphs retain automatic layout selection. Force-directed CoSE layouts now finish synchronously: the library's animated frame loop can still refresh after `stop()`, causing a null-renderer error if React destroys a graph during sample switching or Strict Mode cleanup. The existing bounded iteration count and force-directed algorithm are unchanged. Scheduled resize callbacks check whether the core has been destroyed before using it and refit the graph when the canvas dimensions change, so desktop zoom does not clip nodes after a mobile resize.

`scripts/build_sample_course.py` regenerates both bundles; `--technical-only` leaves the original Computing PDFs untouched. `scripts/verify_public_samples.py` checks manifests, node endpoints, allowed relationship types, source pages/paragraphs, PDF page counts, real local parser extraction, and image-only scan integrity. It records PDF hashes and per-page outcomes in `evaluation/public-samples-verification.json`. CI tests check artifact/reference contracts and report freshness without requiring a Tesseract installation; rerun the actual verification after regenerating PDFs. These fixtures do not call hosted LLMs, load databases, relax upload authorization, or mutate production state. Manually authored answers/graphs must never be reported as measured live output.

Standard, expensive, and login routes have separate fixed-window budgets. Limits live in one process and reset during restart, matching the single-instance deployment boundary. Horizontal scaling requires an external shared limiter before it is safe.

PDF buckets must be private. API credentials should be scoped to the single bucket and provider resources. No provider secret is exposed through Vite variables, API responses, logs, or committed examples.

When demo protection is enabled, a background retention sweep removes READY and FAILED reviewer uploads older than the configured number of days. The configured sample course is explicitly excluded. Cleanup uses the same provenance-scoped deletion path as manual removal: derived Qdrant and Neo4j data first, an unshared source object next, and PostgreSQL metadata last. Active processing rows are never eligible.

## Operational endpoints

Graph retrieval returns `fallback_reason=graph_timeout` when its time budget expires, separately from a connection/other graph failure. If tracking counts record concepts but the selected Neo4j scope contains none, `projection_missing` is reported and the dashboard explains the stored-data mismatch. It does not automatically rebuild a document or consume more LLM quota. This detects complete absence, not every partially missing node.

Vector timing metadata separates `collection_check_ms`, `query_embedding_ms`, `dense_search_ms` and `identifier_lookup_ms`. Local embedding time includes first model load. Hosted query embedding runs inside `query_points`, so its cost is included in `dense_search_ms`, with `embedding_execution=hosted_in_dense_search`; constructing the client `Document` is not a hosted-inference measurement. Reranking/generation remain separately timed. No prompt, key or passage is added to these numeric diagnostics.

Local model clients share a device-selection utility. `LOCAL_MODEL_DEVICE=auto` preserves the prior MPS-if-available choice; explicit `cpu` is useful for reproducible local checks. The local launcher uses CPU with two-thread environment bounds. Hosted Qdrant/Cohere paths are unaffected. Model choice is not a diagnosis that Metal caused a stall.

`GET /api/v1/health` checks PostgreSQL liveness. It is suitable for proving the process can reach its authority store.

`GET /api/v1/ready` requires PostgreSQL, Qdrant and object storage. Combined mode also requires coordinator startup. API-only mode reads worker-presence heartbeats and reports missing workers as degraded ingestion, without disabling queries against existing READY documents. It returns `503` for required dependency/registry failures. Neo4j failure is reported in `degraded_services`, allowing vector retrieval to remain available; this does not guarantee graph ingestion or graph previews can succeed.

Readiness does not prove that paid/provider quota is available for a full LLM request. Provider calls use bounded timeouts where supported and return safe `503` responses at the API boundary.

## Phase 5: resource budgets and separate workers

The original combined process remains the default. `PROCESSING_ROLE=api` does not start ingestion/retention loops; its uploads commit to PostgreSQL and return a deferred dispatch message. `PROCESSING_ROLE=worker` is served by `app/worker.py`, not Uvicorn. It owns the same coordinator, processor, heartbeat, checkpoints, and retention path as combined mode. Worker presence is separate from document leases: it expires using database time and communicates liveness, not proof that every backing integration is configured identically. API and workers must use matching namespaces/models/storage and shared-limit settings.

Claiming uses `FOR UPDATE SKIP LOCKED`; current-attempt mutations hold a row lock through their transaction so recovery cannot replace the task between its token check and update. Advisory locking serializes idempotent schema startup and resource admission. The in-memory queues remain bounded dispatch caches, not the durable job store. Restart recovery creates a new attempt while preserving validated graph checkpoints. Attempt/lease fencing and READY eligibility are retained; this is at-least-once processing, not distributed exactly-once delivery.

`resource_budget_service.py` accounts retained pages/source bytes, chunk reservations, and active jobs. Admission writes `page_count`, `source_bytes`, and `reserved_chunk_count`; the processor rechecks aggregate capacity excluding its own row and persists the actual count before indexing. This may conservatively reject a request while another transaction is finishing. Pending work and the legacy PDF guard are optional/additional installation limits; per-worker concurrency, OCR work, rendering, and graph batch limits remain separate. Capacity exhaustion is retryable within the existing attempt budget; a per-document overflow requires splitting the input or changing its document budget. Deletion frees capacity only after its existing cross-system cleanup succeeds and metadata commits.

Unknown legacy inventory is never treated as zero usage. `refresh_resource_inventory.py` reads retained READY/FAILED PDFs, reports aggregate results by default, and uses the admission lock plus a row/state/key recheck before an explicit local-only `--apply`. It changes only page/byte/reservation metadata, not source contents, embeddings, graphs, or authentication. Source bytes and chunk reservations are not a full estimate of index/database/object-store overhead; historical shared-object records may be counted more than once conservatively.

Blocking thread operations in the processor and active generation wrappers drain before their caller releases a lease, including repeated cancellation. Python cannot retract a running thread/remote request just by cancelling an await. Graceful shutdown may therefore wait for an in-flight operation; remote hard-kill/ambiguous-commit behavior is not made transactional by this helper. Existing cleanup and checkpoint recovery remain necessary.

`shared_provider_service.py` uses a short PostgreSQL transaction and per-scope/provider advisory lock to reserve minute/day request counts before an active graph/answer/exam generation attempt. UTC windows use database time. All related processes must enable this service and share its configuration. Reservation-storage failures fail closed; quota errors continue through existing provider failover or evidence fallback. Counts survive process restarts and conservatively include failed/cancelled reservations. They are request limits, not token, money, or provider-plan quotas. Provider usage outside this database/scope and embedding/reranking services are not covered. Process-local circuit breakers and HTTP/login limiters are still process-local; this does not authorize unrestricted horizontal API replication.

`GET /ingest/uploads/page` retains representative active/ready/failed ordering in SQL, then uses an opaque `(created_at, upload_id)` cursor and returns at most 100 rows plus a next cursor. The old `/uploads` list remains compatible. The UI requests 25 at a time, ignores stale responses, resets paging after a new upload, and labels filters/counts as page-local. SQL ranking may still scan the corpus. `graph_display_service.py` preserves query anchors first, bounds unique visual nodes/edges, and drops edges whose endpoints are outside the subset. It does not change the underlying stored graph or READY document scope, and it is not a complete server-side traversal-complexity bound.

The ingestion benchmark exercises real isolated PostgreSQL admission, leases, state transitions, checkpoint recovery, stale-token rejection, canonical pagination, and endpoint deletion alongside real local parsing/embeddings/Qdrant. Source storage and Neo4j/LLM work are test doubles. It verifies the PostgreSQL/coordinator contracts without spending provider quota or modifying production. Its report states these limits; use live backing-store failure injection and multi-process hard-kill tests before making stronger production consistency or capacity claims. Phase 6 workspace ownership/authorization remains separate and unimplemented.

## Change checklist

Before merging a processor change:

1. preserve the stage order and READY gate;
2. keep task-token and lease-owner fencing on every worker update;
3. make new derived writes upload-provenance-scoped and idempotent;
4. define compensation for partial success;
5. keep queue saturation durable rather than returning a false processing failure;
6. add success, interruption, failure, and stale-worker tests;
7. rerun backend compile/tests and the frontend production build;
8. update `.env.example`, `render.yaml`, and operator docs for new configuration;
9. do not deploy or push until explicit authorization is given.
