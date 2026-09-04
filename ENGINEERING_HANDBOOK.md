# ConceptGraph Engineering Handbook

This document describes the portfolio edition's runtime invariants. Setup, deployment inputs, capacity evidence, and operator commands live in `README.md`.

## System boundaries

The React SPA calls one FastAPI service. FastAPI owns both HTTP handling and a bounded background coordinator. PostgreSQL is the only authoritative state store. Qdrant vectors and Neo4j graph entities are derived, provenance-scoped projections. S3-compatible object storage holds immutable PDF sources.

External AI services are deliberately provider-configurable:

- graph extraction, synthesis, and exams: Groq by default, with optional Cerebras failover;
- embeddings: local MiniLM or Qdrant Cloud Inference;
- reranking: local cross-encoder or Cohere;
- Gemini is an optional LLM alternative.

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

Graph extraction is a bounded section-batch workflow rather than a multi-agent system. Chunks are grouped by detected section, each section contributes beginning/middle/end evidence, and a fair selector distributes the configured request budget across the document. Production defaults allow four chunks per request and at most six graph requests per PDF. Each batch asks for at most eight concepts and ten relationships and supplies stable source chunk IDs.

Provider output is schema-validated, limited to six relationship types (`PREREQUISITE_OF`, `PART_OF`, `EXPLAINS`, `RELATED_TO`, `CAUSES`, and `APPLIES_TO`), checked for valid endpoints, deduplicated using lowercase whitespace-free concept names, and rejected at node level when it cites an unknown source chunk. Strict JSON Schema is attempted first; a smaller JSON-object request is the compatibility fallback, and both responses still pass local Pydantic validation. Successful batches are merged deterministically, so one malformed or timed-out section does not discard completed graph work.

Quality is reported separately from document readiness. Two or more concepts with at least one valid relationship are `GRAPH_READY`; a non-empty graph below that threshold is `GRAPH_PARTIAL`; zero retained concepts is `READY_WITHOUT_GRAPH`. All three documents remain vector-searchable, so an empty graph is visible without falsely turning successful PDF indexing into a processing failure.

Each retained Neo4j concept stores upload ID, PDF filename, source chunk ID, page number, and detected section heading. Graph retrieval returns those properties to the dashboard, where a selected concept can open the original PDF at its source page.

Text extraction currently uses PyMuPDF's native text layer. An image-only/scanned PDF is rejected with a safe OCR-required error before vector or graph writes. OCR is a documented future extension, not a currently claimed capability.

## LLM resilience and failover

`LLM_PROVIDER=groq` keeps Groq as the primary provider. When `CEREBRAS_API_KEY` is configured, graph extraction, answer synthesis, and exam generation can fail over to Cerebras `gpt-oss-120b` after a Groq quota response, timeout, connection failure, or provider-side server error. Graph extraction can also use Cerebras when Groq cannot produce a valid structured graph for a batch.

A process-wide, thread-safe circuit breaker records a provider cooldown, defaulting to 300 seconds. Requests bypass a cooling provider instead of repeatedly spending latency and quota on a failure that is expected to recur. Provider SDK retries are disabled on the Groq calls covered by failover so routing happens promptly.

Cerebras uses its OpenAI-compatible chat-completions endpoint. Strict structured output omits unsupported array-size schema keywords, while prompt limits and local validation still enforce the application's bounded graph contract. Provider response bodies and credentials are never copied into public errors.

If both providers are unavailable:

- graph processing preserves completed batches and may finish as `GRAPH_PARTIAL` or `READY_WITHOUT_GRAPH` because vector search remains useful;
- answer synthesis returns retrieved, citation-labelled evidence instead of inventing an answer;
- exam generation returns a safe temporary-unavailability response.

## Idempotency and compensation

Each execution has an execution token. Before processing starts, Qdrant points and Neo4j nodes for the upload are removed. New chunk payloads, graph nodes, and relationships carry upload provenance and the execution token.

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
2. checks the global three-attempt cap;
3. creates a new attempt and task token when budget remains;
4. resets the current stage to `UPLOADED` and clears lease metadata;
5. queues the durable candidate when memory capacity is available.

Manual retry uses the same attempt cap and creates a new task token. Retry cannot proceed for a permanent document/configuration failure or a missing source object. An old status URL remains useful because task lookup can resolve attempt history to the document's current attempt.

## Retrieval and evidence

Query/exam flows first resolve canonical course context from READY documents only. Failed and active uploads cannot contribute vectors, graph metrics, questions, or answers.

Graph retrieval uses parameterized, read-only Cypher scoped to canonical course IDs and READY document IDs. Native driver records preserve relationship direction. Each matched concept is an anchor for an inbound `PREREQUISITE_OF` traversal with a hard maximum of two hops. Direct and foundational prerequisite names are deduplicated separately before they expand the semantic query; all valid typed relationships remain visible in the concept map. Breadth is bounded by five anchors and depth is bounded by two, preventing an uncontrolled whole-graph expansion.

When no term-matched anchor exists, the service may return a bounded course graph for visualization, but it does not use that broad graph to expand the semantic query. Qdrant receives the original question unchanged. This keeps graph absence or vocabulary mismatch from reducing the vector-search baseline.

The production retrieval path deliberately uses a deterministic Cypher template rather than executing LLM-generated database queries. A legacy provider-backed Cypher helper remains isolated from the request path, but every executed query is validated as read-only and parameterized. This reduces injection risk and makes course/document scoping explainable.

Qdrant search filters by READY upload IDs. Its query text contains the original question plus separately labelled direct and foundational prerequisite terms only when an anchor was found. Hosted and local embeddings use the same 384-dimensional MiniLM space and normalized cosine collection contract. Startup rejects an incompatible collection and instructs the operator to choose a new collection name.

Reranking returns a provider-neutral logit. Cohere probabilities are converted to logits so the existing sigmoid-based evidence gate behaves identically. Low evidence returns a grounded fallback without asking the synthesis LLM to invent an answer. User-facing sources expose document, page, section, and supporting passage but not internal vector IDs or file keys.

## Security model

This is a shared-secret portfolio demo, not multi-user authentication. Public deployments require an access token of at least 24 characters. Login compares secrets in constant time and issues a signed, expiring, HttpOnly cookie. Secure cookies and exact CORS origins are required in production.

The frontend does not trust the login response alone. It performs a separate session-status request and unlocks protected controls only when the API validates the signed cookie. The reviewer code exists only in deployment secrets and is never compiled into the SPA or published in documentation.

Unauthenticated visitors receive one configured, pre-uploaded READY course through a dedicated read-only endpoint. That endpoint returns the bounded concept graph and permits PDF previews only for documents already belonging to that course; it cannot invoke LLM, upload, query, exam, retry, or deletion operations. Public sample reads have their own IP-fingerprinted process-local rate limit.

Standard, expensive, and login routes have separate fixed-window budgets. Limits live in one process and reset during restart, matching the single-instance deployment boundary. Horizontal scaling requires an external shared limiter before it is safe.

PDF buckets must be private. API credentials should be scoped to the single bucket and provider resources. No provider secret is exposed through Vite variables, API responses, logs, or committed examples.

When demo protection is enabled, a background retention sweep removes READY and FAILED reviewer uploads older than the configured number of days. The configured sample course is explicitly excluded. Cleanup uses the same provenance-scoped deletion path as manual removal: derived Qdrant and Neo4j data first, an unshared source object next, and PostgreSQL metadata last. Active processing rows are never eligible.

## Operational endpoints

`GET /api/v1/health` checks PostgreSQL liveness. It is suitable for proving the process can reach its authority store.

`GET /api/v1/ready` checks PostgreSQL, Qdrant, Neo4j, object storage, and coordinator startup. It returns `503` with dependency names when traffic should not be routed to the instance.

Readiness does not prove that paid/provider quota is available for a full LLM request. Provider calls use bounded timeouts where supported and return safe `503` responses at the API boundary.

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
