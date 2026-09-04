# ConceptGraph technical-interview codebase audit

Audited against repository commit `c7969aa` on 2026-09-04. This document describes the code that exists in this repository; it does not treat README aspirations as implementation evidence.

## Accuracy legend

- **Implemented** means an executable path and, where applicable, a test exist.
- **Partial** means a useful version exists but has an important limitation.
- **Not implemented** means the repository contains no working path for the claim.
- Production defaults below mean the Render Blueprint in `render.yaml`; local defaults can differ.

## 1. Project overview

1. **Problem solved — implemented.** ConceptGraph turns one or more course PDFs into searchable evidence and an LLM-extracted concept graph, then answers course-scoped questions with PDF/page citations. The API orchestration is in `app/api/endpoints/ingest.py` and `query.py`; processing is in `app/services/document_processing_service.py`; retrieval is in `rag_service.py`; the user experience is in `src/pages/Dashboard.tsx`.

2. **User-facing features.** The implemented UI supports PDF upload, live processing stages, retry and deletion, course selection, grounded Q&A, confidence/evidence cards, PDF preview, interactive concept graphs, source provenance, and practice exams (`Dashboard.tsx`, `UploadModal.tsx`, `PdfPreviewModal.tsx`, `ConceptGraphCanvas.tsx`, `ExamPanel.tsx`). `DemoAccessGate.tsx` adds a shared reviewer code and `PublicSampleCourse.tsx` exposes one read-only sample.

3. **Technology stack.** React 18, TypeScript 5, Vite 5, Tailwind CSS, Cytoscape.js, React Markdown and Lucide are declared in `package.json`. FastAPI, Pydantic 2, SQLAlchemy async/asyncpg, PyMuPDF, qdrant-client, Neo4j async driver, boto3, Groq and httpx are pinned in `requirements.txt`. PostgreSQL stores control state, Qdrant vectors, Neo4j graph data, and S3-compatible storage PDFs. Production uses Render, Qdrant Cloud inference, Cohere Rerank, Groq with optional Cerebras failover, Neo4j Aura-compatible credentials, and R2/S3-compatible storage (`render.yaml`).

4. **High-level architecture.** The static React frontend calls one FastAPI process. FastAPI persists admissions and state in PostgreSQL, schedules work through an in-process bounded `ProcessingCoordinator`, stores the source in object storage, chunks with PyMuPDF, indexes vectors in Qdrant, stores concepts in Neo4j, and calls hosted AI providers (`app/main.py`, `core/processing_coordinator.py`, `services/*`). This is a modular monolith, not microservices.

5. **End-to-end flow.** `POST /api/v1/ingest/upload` validates and stores a PDF, creates `DocumentUpload`/`ProcessingAttempt`, and submits its durable ID. A worker calls `DocumentProcessingService.process_document`: cleanup → extraction → chunking → Qdrant embeddings/upsert → graph extraction/validation → Neo4j write → READY. `POST /api/v1/query` resolves only READY documents, `RetrievalService.retrieve` gets bounded one-hop and two-hop prerequisite context plus 10 Qdrant candidates, `RerankService` reranks them, `citation_service` thresholds/deduplicates the evidence, and `SynthesisService` produces or degrades to a cited answer.

6. **Main responsibilities.** Routers in `app/api/endpoints/` translate HTTP to services. `UploadService` owns PostgreSQL state/attempts; `CourseService` canonicalizes and gates courses; `StorageService` owns PDF bytes; `ParserService` extracts/chunks; `IngestionService` owns embeddings, Qdrant, LLM graph extraction and Neo4j writes; `RetrievalService` combines Neo4j and Qdrant; `RerankService`, `citation_service`, and `SynthesisService` rank, accept and answer; `ExamService` creates exams; coordinator/retention services own background loops.

7. **Synchronous versus asynchronous.** FastAPI handlers and SQLAlchemy/Neo4j operations are async. Blocking PyMuPDF, Qdrant SDK, boto3, local model and Groq/Cerebras calls are moved to threads with `asyncio.to_thread` in `ingest.py`, `document_processing_service.py`, `rag_service.py`, `exam_service.py`, and `synthesis_service.py`. Ingestion and retention run as in-process asyncio background tasks; there is no external worker queue.

## 2. PDF ingestion pipeline

8. **Internal upload work.** `upload_document` in `app/api/endpoints/ingest.py` checks extension, MIME, declared and actual length, `%PDF-` signature, encryption and page count; hashes bytes; takes a PostgreSQL advisory transaction lock; gets/creates a course; detects duplicates and enforces the global count; writes the content-addressed object; commits the upload/attempt rows; then calls `processing_coordinator.submit`.

9. **Upload endpoint.** `POST /api/v1/ingest/upload`, function `upload_document`, accepts multipart `course_id` and `file` and returns HTTP 202 `IngestResponse` (`ingest.py`, `schemas/ingest.py`). It is protected by `DemoProtectionMiddleware` when the shared demo token is configured.

10. **File storage.** `StorageService.put_pdf` stores bytes under `courses/{course_uuid}/documents/{sha256}.pdf` (`storage_service.py`). Production selects the S3 backend and supplies an R2/S3-compatible endpoint in `render.yaml`; local development can write below `data/uploads/objects`. PostgreSQL stores the opaque key in the legacy-named `stored_file_path` column.

11. **Text extraction.** `ParserService.extract_pages_from_bytes` opens bytes with PyMuPDF and calls `page.get_text("text")`; empty pages are skipped (`parser_service.py`). **OCR is not implemented**: an image-only PDF eventually fails with “Scanned PDFs need OCR.”

12. **Malformed/encrypted/invalid handling.** `upload_document` rejects bad suffix/MIME/signature, empty input, password protection, zero pages and PyMuPDF open errors with 400; oversize is 413 (`ingest.py`). A syntactically valid scanned PDF passes admission but becomes permanent `DOCUMENT_ERROR` when chunking yields nothing (`document_processing_service.py`, `core/processing.py`).

13. **Size limit.** Default is 10 MiB from `MAX_PDF_SIZE_MB` (`core/config.py`). `ingest.py` checks `UploadFile.size` and still reads at most `MAX_UPLOAD_BYTES + 1`, preventing a false declared size from bypassing enforcement. `UploadModal.tsx` also performs a 10 MiB UX check, but the backend is authoritative.

14. **Duplicate detection.** Duplicates are scoped to canonical course UUID plus SHA-256. `UploadService.find_duplicate` returns the newest matching row, and the endpoint returns that existing task with `duplicate=true` rather than creating a new one (`ingest.py`, `upload_service.py`).

15. **SHA-256 implementation.** The endpoint computes `hashlib.sha256(content).hexdigest()` over the full admitted byte array and stores it in `DocumentUpload.content_hash`; it also becomes part of `StorageService.object_key` (`ingest.py`, `storage_service.py`). Therefore byte-identical PDFs deduplicate; visually identical PDFs with changed metadata do not.

16. **Simultaneous duplicates.** The endpoint obtains `pg_advisory_xact_lock(hashtext('conceptgraph:pdf-admission'))` before course creation/deduplication (`ingest.py`). Because it is a transaction-scoped, database-wide admission lock, cooperating API instances serialize all uploads until commit. There is no unique `(course_uuid, content_hash)` constraint, so a writer bypassing this endpoint could still create duplicates.

17. **Locking mechanisms.** Admission uses the global advisory lock above. Worker claiming and deletion use `SELECT ... FOR UPDATE [SKIP LOCKED]` in `UploadService.claim_for_processing`, `prepare_stale_recoveries`, `retry_upload`, and `lock_for_deletion`. Leases (`lease_owner`, `lease_expires_at`) plus task IDs fence attempts. This is application/database locking, not a distributed queue lock.

18. **Document stages/statuses.** `ProcessingStage` defines `UPLOADED`, `EXTRACTING`, `EXTRACTED`, `CHUNKING`, `CHUNKED`, `EMBEDDING`, `EMBEDDED`, `BUILDING_GRAPH`, `GRAPH_BUILT`, `READY`, `FAILED`, `CANCELLED` (`core/processing.py`). Separately, the coarse `status` column is `active`, `ready`, `failed`, or legacy/cancelled. `CANCELLED` is defined but no normal cancellation endpoint implements it.

19. **State machine.** `DocumentProcessingService.process_document` advances the happy path in the order listed above through `UploadService.set_stage`; any caught failure calls cleanup and `mark_failed`. Manual retry or stale recovery creates a new task ID/attempt and resets to `UPLOADED`. There is no formal transition table preventing arbitrary forward jumps; correctness depends on service call order plus task/lease fencing.

20. **READY conditions.** `UploadService.mark_completed` requires the current active attempt, current stage `GRAPH_BUILT`, a canonical course UUID, a nonempty storage key, positive committed chunk count, and one valid `GraphStatus`. Immediately before it, `process_document` also verifies the source object still exists (`upload_service.py`, `document_processing_service.py`). A graph may be empty and still become READY if vectors exist and graph status is `READY_WITHOUT_GRAPH`.

21. **READY gating.** Write-side gating is `mark_completed`; read-side gating is `CourseService.get_ready_context`, which selects only `stage == READY` and `processed_chunk_count > 0` and deduplicates by content hash (`course_service.py`). Both query and exam routers call it before touching retrieval.

22. **Partial-stage failure.** `process_document` classifies the exception, attempts upload-scoped Qdrant/Neo4j cleanup, then records FAILED with a safe category/message (`document_processing_service.py`). The source PDF and PostgreSQL record remain to support retry or deletion. Cleanup failure is logged, so orphan derived artifacts can remain.

23. **Crash halfway through.** The worker heartbeat extends a 180-second default lease every 30 seconds. On restart, `ProcessingCoordinator._recover_and_fill` calls `UploadService.prepare_stale_recoveries`, which turns expired non-UPLOADED work into a failed historical attempt and a new fenced attempt, up to three total attempts (`processing_coordinator.py`, `upload_service.py`). Each execution begins by removing upload-scoped external outputs, making replay mostly idempotent.

24. **Retryable versus permanent.** `classify_failure` treats provider quota/timeouts, connectivity/storage/database errors, malformed provider JSON and worker interruption as retryable. Missing/invalid provider configuration, unavailable model, encrypted/malformed/no-text PDF, missing source object and most unknown errors are permanent (`core/processing.py`). Classification is string-based, so it is practical but brittle.

25. **Retry implementation.** The user calls `POST /ingest/uploads/{upload_id}/retry`; `UploadService.retry_upload` row-locks a retryable FAILED record, enforces the attempt cap, assigns a new `task_id`, inserts `ProcessingAttempt`, verifies the source, and submits/defer-persists it (`ingest.py`, `upload_service.py`). Restart recovery performs a similar automatic reset for expired active attempts.

26. **Maximum retries.** `MAX_PROCESSING_ATTEMPTS = 3` means at most three total attempts, not three retries after the first (`core/processing.py`, `upload_service.py`). At the cap, `mark_failed` makes the error terminal and instructs removal/re-upload.

27. **Exponential backoff.** **Not implemented** for document attempts. Manual retry is immediate, and restart recovery is driven by lease expiry/dispatcher sweeps. boto3 has its own standard SDK retries, but that is not pipeline exponential backoff (`storage_service.py`).

28. **Compensating cleanup.** `IngestionService.cleanup_upload` deletes Qdrant points filtered by `upload_id`, Neo4j concepts scoped by course/upload, and an orphan Neo4j course. It runs before every processing attempt and after failures (`ingestion_service.py`, `document_processing_service.py`). It cannot atomically coordinate all databases.

## 3. Chunking and embeddings

29. **Text split.** `ParserService.chunk_pages` processes each extracted page independently, heuristically splits it into heading/body sections, then splits each section on non-whitespace word spans (`parser_service.py`). Chunk IDs are `{upload_id}:{page}:{page-local-index}`.

30. **Strategy.** It is fixed-size, page-bounded, word-count chunking with overlap; it is not semantic, sentence-aware or token-aware. `_looks_like_heading` recognizes uppercase/title-case/numbered short lines and adds the heading to metadata.

31. **Size and overlap.** Constructor defaults are 500 words with 50-word overlap, so the sliding step is 450 words (`ParserService.__init__`, `_split_text`).

32. **Reason for values.** No benchmark or design comment justifies 500/50 in code. In an interview, call them pragmatic defaults that preserve page citations and boundary context, not tuned optimal values.

33. **Embedding provider/model.** Production `render.yaml` sets `EMBEDDING_PROVIDER=qdrant_cloud`, model `sentence-transformers/all-MiniLM-L6-v2`. Local mode lazily loads `SentenceTransformer` from the optional `requirements-local-models.txt` (`IngestionService.embedding_model`).

34. **Dimension.** Production and default configuration expect 384 dimensions (`EMBEDDING_DIMENSION` in `core/config.py` and `render.yaml`). `validate_qdrant_collection` rejects a collection with another size.

35. **Where generated.** `IngestionService.upsert_chunks_to_qdrant` sends Qdrant `Document(text, model)` objects for Cloud Inference; local mode calls `SentenceTransformer.encode` (`ingestion_service.py`). Query embeddings follow the same provider choice in `RetrievalService.search_qdrant` (`rag_service.py`).

36. **Batching/synchrony.** All chunks for one document are passed together to `upsert_chunks_to_qdrant`; local `encode` is batched by the library, while Qdrant Cloud receives a list of inference documents in one upsert. The blocking function runs in a worker thread from the async pipeline (`document_processing_service.py`). There is no explicit application batch-size/pagination for very large vector upserts.

37. **Embedding failures.** A short/zero upsert count is treated as failure; provider/Qdrant exceptions flow to `classify_failure`, cleanup, and retryable FAILED state (`document_processing_service.py`). Because Qdrant upsert is idempotent by deterministic point ID, replay overwrites the same points.

38. **Vector metadata.** Each payload contains `chunk_id`, `chunk_index`, canonical course UUID under `document_id`, `upload_id`, `document_name`, `page_number`, `section_heading`, `execution_token`, and full `text` (`parser_service.py`, `document_processing_service.py`, `ingestion_service.py`).

39. **Ownership representation.** Course/document scope is represented by canonical course UUID and upload UUID in payload. **No user/tenant identity is present** because the app is a shared portfolio demo, not multi-tenant auth (`parser_service.py`, `DemoAccessGate.tsx`).

40. **Similarity metric.** The one collection is created with `Distance.COSINE` and `VectorParams(size=384)` (`IngestionService._ensure_qdrant_collection`). Local embeddings are explicitly normalized; Qdrant Cloud model behavior is delegated to the hosted inference model.

41. **Retrieval filters.** Question retrieval applies a Qdrant `MatchAny` filter on `upload_id` for the selected course's READY document IDs. Exam generation uses the same upload filter while scrolling. There is no direct course/user payload filter; the READY IDs originate in PostgreSQL (`rag_service.py`, `exam_service.py`, `course_service.py`).

## 4. Qdrant

42. **Purpose.** Qdrant provides approximate semantic retrieval over chunk embeddings; PostgreSQL is not used for vector or full-text search. `RetrievalService.search_qdrant` turns a question plus graph terms into a vector and returns relevant passages (`rag_service.py`).

43. **Collections.** The application uses one configured collection, default `conceptgraph_chunks`; Render overrides it to `conceptgraph_portfolio_chunks` (`core/config.py`, `render.yaml`). It does not create per-course collections.

44. **Schema.** `_ensure_qdrant_collection` creates an unnamed 384-dimensional cosine vector and `_ensure_qdrant_payload_indexes` creates a KEYWORD index only for `upload_id` (`ingestion_service.py`). It validates existing vector size and rejects named/unsupported vectors.

45. **Point contents.** A `PointStruct` contains a deterministic UUID point ID, either a concrete float vector or a Qdrant inference `Document`, and the full chunk text plus metadata described in Q38 (`IngestionService.upsert_chunks_to_qdrant`).

46. **Point IDs.** `_qdrant_point_id` computes UUIDv5 with `uuid.NAMESPACE_URL` from the chunk ID. Since chunk ID includes upload/page/index, retries overwrite consistently while separate uploads remain distinct (`ingestion_service.py`).

47. **Filtering scope.** Query and exam filters use PostgreSQL-derived READY `upload_id` values. This prevents cross-course retrieval for a correct ready context but does **not** provide per-user isolation (`CourseService.get_ready_context`, `rag_service.py`, `exam_service.py`).

48. **Exact search flow.** `RetrievalService.retrieve` first runs Neo4j keyword matching; prerequisite names are appended to the question; the expanded text is embedded locally or by Qdrant Cloud; `query_points` requests 10 cosine candidates with payload and upload filter; results become `{id, score, text, metadata}` (`rag_service.py`). The query router then sends all candidates to the reranker.

49. **Initial candidate count.** Default `top_k=10` in `RetrievalService.retrieve/search_qdrant`. No endpoint parameter exposes it (`rag_service.py`, `query.py`).

50. **Hybrid search.** **Not implemented.** The final query contains graph-added terms, but Qdrant performs one dense-vector search, not dense+sparse fusion.

51. **Lexical/BM25.** **Not implemented.** Neo4j uses substring matching for concept selection; this is not document BM25 (`RetrievalService._fallback_cypher`).

52. **Metadata filtering.** **Implemented** with `upload_id MatchAny` on every Q&A/exam retrieval and a Qdrant KEYWORD payload index (`rag_service.py`, `exam_service.py`, `ingestion_service.py`).

53. **Protected Qdrant edge cases.** Code handles collection-not-found as empty retrieval/cleanup, old client method differences, create races/409s, incompatible dimensions, named vectors, missing payload index, deterministic retry upserts, invalid Cohere indexes, and upload-scoped deletion. Relevant tests are in `tests/test_processing.py` (`test_incompatible_qdrant_dimension_is_rejected`, cloud inference, payload index, DNS and cleanup tests).

54. **Point deletion.** `IngestionService.cleanup_upload` issues `vector_client.delete` with a `FilterSelector` matching `upload_id` and `wait=True`. Both manual deletion and failure/retry cleanup call it (`ingestion_service.py`, `ingest.py`, `document_processing_service.py`).

## 5. Neo4j and the knowledge graph

55. **Why Neo4j.** Neo4j stores explicit concept relationships that dense vectors do not model, exposes prerequisite direction for query expansion, and supplies the interactive graph (`ingestion_service.py`, `rag_service.py`, `ConceptGraphCanvas.tsx`). Its role is useful but deliberately bounded: two-hop prerequisite traversal, not advanced graph algorithms.

56. **Node labels.** Only `Course` and `Concept` labels are written. `IngestionService.store_graph_extraction` creates `(Course)-[:CONTAINS]->(Concept)` (`ingestion_service.py`).

57. **Relationship types.** Structural `CONTAINS` plus six LLM graph types: `PREREQUISITE_OF`, `PART_OF`, `EXPLAINS`, `RELATED_TO`, `CAUSES`, `APPLIES_TO` (`schemas/extraction.py`). Dynamic relationship type interpolation is guarded by `_safe_relationship_type`.

58. **Concept properties.** Neo4j concepts store scoped `id`, original `source_id`, `name`, `normalized_name`, `type`, `description`, primary and aggregated chunk/page/section provenance, `course_id`, `upload_id`, `execution_token`, and `document_name` (`IngestionService.store_graph_extraction`).

59. **Edge properties.** LLM edges store `relation_type`, `course_id`, `upload_id`, `execution_token`, and `document_name`; `CONTAINS` has no custom properties (`ingestion_service.py`). Direction is preserved by matching a scoped source and target before `MERGE`.

60. **Concept extraction.** `IngestionService.extract_graph_from_chunks` groups sampled chunks by document/section, chooses at most the configured six batches, formats source-labelled excerpts, calls an LLM for each batch, attaches only valid provenance, and merges normalized concepts (`ingestion_service.py`).

61. **Relationship generation.** The same LLM response contains source node ID, target node ID and one permitted type. Pydantic checks endpoints, normalizes types, drops unsupported/self/duplicate relations, and the store method rechecks the type (`schemas/extraction.py`, `ingestion_service.py`).

62. **LLM usage.** **Implemented.** Groq, Cerebras or optional Gemini extracts concepts/edges; Groq→Cerebras failover applies when `LLM_PROVIDER=groq` (`IngestionService.extract_graph_from_text`, `_extract_with_failover`). There is no deterministic NLP fallback that builds a graph without an LLM.

63. **Prompt/schema.** `_extraction_system_prompt` requires stable snake-case IDs, real source chunk IDs, valid endpoints, the six types, ≤8 concepts and ≤10 relationships per batch. `_graph_extraction_schema` uses strict JSON Schema; Groq/Cerebras retry once with smaller context/JSON-object mode and still validate with `GraphExtractionResponse` (`ingestion_service.py`, `schemas/extraction.py`).

64. **Duplicate concepts.** Within one response, `GraphExtractionResponse.validate_graph_integrity` canonicalizes by normalized name; across batches, `_merge_graph_extractions` keeps the first ID and aggregates provenance. Across different uploads, Neo4j IDs remain distinct, so same-name course concepts are **not** entity-resolved across PDFs (`schemas/extraction.py`, `ingestion_service.py`).

65. **Normalization.** `normalize_concept_name` case-folds and removes all whitespace. It does not remove punctuation, stem words, resolve acronyms or compare embeddings, so “machine-learning” and “machine learning” can remain separate (`schemas/extraction.py`).

66. **Course identity.** Neo4j `Course.id` normally uses the deterministic PostgreSQL UUIDv5. `ReadyCourseContext.graph_course_ids` includes the UUID and legacy display aliases for backward compatibility (`course_service.py`). New concept IDs are scoped as `{course_uuid}:{upload_id}:{llm_concept_id}`.

67. **Provenance.** `_attach_provenance` discards concepts whose cited chunk ID is absent, then adds filename, page, section and upload ID; merging retains arrays of chunks/pages/headings. Neo4j persists them and the UI opens `/ingest/uploads/{upload_id}/preview#page=N` (`ingestion_service.py`, `ConceptGraphCanvas.tsx`, `Dashboard.tsx`).

68. **Cross-course isolation.** Concept IDs are course/upload scoped, each concept is attached to a specific Course, and every Cypher query filters course aliases and READY upload IDs (`ingestion_service.py`, `rag_service.py`). This isolates courses, but there is no separate user boundary.

69. **Prerequisite direction.** Yes: source `PREREQUISITE_OF` target means source is the prerequisite. `_build_graph_result` identifies a prerequisite only when the selected concept is the target (`rag_service.py`), and the UI walks incoming prerequisite edges (`ConceptGraphCanvas.tsx`).

70. **Traversal in Q&A.** `_fallback_cypher` selects up to five concepts whose names contain question terms and uses one undirected adjacent `OPTIONAL MATCH`; `_build_graph_result` extracts inbound prerequisites and appends their names to the vector query (`rag_service.py`). It is not a general path traversal.

71. **Hop count.** Backend retrieval retains both **one-hop direct prerequisites** and **two-hop foundational prerequisites** for each matched concept. The query limits graph depth to two and anchor count to five; the frontend shows each depth with distinct styling. Its longer click animation only traverses the already returned subgraph and does not increase server retrieval depth (`rag_service.py`, `ConceptGraphCanvas.tsx`).

72. **Prerequisite selection.** Only adjacent relationships of type `PREREQUISITE_OF` where the neighbor is the source and matched concept is the target become `prerequisite_names` (`RetrievalService._build_graph_result`). Names are deduplicated and sorted.

73. **Graph role.** It is used for both visualization and retrieval: prerequisite names alter the dense query, and bounded graph JSON is included in the answer prompt. However, document evidence still comes solely from Qdrant, so the graph does not independently supply quoted passages (`rag_service.py`, `synthesis_service.py`).

74. **Neo4j unavailable.** Query retrieval fails before Qdrant and `query.py` returns 503; ingestion also fails/retries while building/cleaning the graph. **No vector-only runtime fallback is implemented**, although a graph-empty response works when Neo4j itself is reachable (`rag_service.py`, `document_processing_service.py`).

75. **Graph deletion.** `cleanup_upload` matches the Course, `CONTAINS`, and Concept `upload_id`, then `DETACH DELETE`s those concepts and removes the course if it contains none (`ingestion_service.py`). Relationships attached to deleted concepts are removed by `DETACH`.

76. **Relevant bug fixes.** Every store/retrieve/cleanup operation opens `async with graph_driver.session()` and native Neo4j `Record` values are preserved because `.data()` loses relationship endpoint objects (`ingestion_service.py`, `rag_service.py`). Scoped IDs and upload/execution provenance prevent collisions/stale-attempt ambiguity. Tests include driver relationship mapping, bidirectional direction and provenance cases in `tests/test_processing.py`.

## 6. GraphRAG

77. **Why “GraphRAG” is supportable.** The question is matched against Neo4j concepts, inbound prerequisite paths are traversed for one and two hops, those separately labelled terms expand the Qdrant query, and graph JSON joins retrieved passages in the LLM prompt (`rag_service.py`, `synthesis_service.py`). That is graph-assisted retrieval augmentation, though simpler than research-grade GraphRAG.

78. **Normal vector RAG part.** PDF chunking/embedding, cosine top-10 search, Cohere/local reranking, evidence thresholding, five source passages, and LLM synthesis are conventional RAG (`parser_service.py`, `ingestion_service.py`, `rag_service.py`, `rerank_service.py`, `citation_service.py`, `synthesis_service.py`).

79. **Graph addition.** Neo4j keyword-selects concepts, retrieves adjacent nodes/edges, identifies inbound prerequisites, expands the vector query, and supplies structured context for response/visualization (`RetrievalService.execute_graph_retrieval`, `_build_expanded_query`).

80. **Complete GraphRAG query.** `query_conceptgraph` resolves a READY course → `execute_graph_retrieval` runs parameterized deterministic Cypher → direct and two-hop prerequisite names expand the question → Qdrant returns 10 filtered chunks → reranker scores all → evidence threshold keeps useful chunks → five sources are built → `SynthesisService._user_prompt` combines question, ≤6,000 graph characters and those sources → Groq/Cerebras/Gemini returns text (`query.py` and services above).

81. **Combining results.** Graph and vector scores are not numerically fused. Graph output changes the embedding input by string appending, while the final rank is `0.7 * sigmoid(rerank_logit) + 0.3 * cosine_score` (`rag_service.py`, `citation_service.py`).

82. **Graph expansion type.** Retrieval may return all adjacent relation types for visualization/context, but only inbound `PREREQUISITE_OF` names expand the vector query. Related/part-of/causes names are not query-expansion terms (`rag_service.py`).

83. **Graph to text.** The backend does not map graph nodes back to source chunks for retrieval. It serializes graph dictionaries directly into bounded JSON in `SynthesisService._user_prompt`; source text still comes from Qdrant (`synthesis_service.py`).

84. **When GraphRAG runs.** Every protected `POST /query` attempts Neo4j retrieval. If no term-matched anchor exists, `_fetch_course_graph` may load up to 50 concepts for visualization, but no broad-graph names are added to the Qdrant query; retrieval preserves the original vector-search question (`rag_service.py`).

85. **Weaknesses.** Exact substring concept matching, no stopword removal, a fixed two-hop ceiling, only prerequisites used for expansion, no learned graph/vector fusion, possible LLM-extracted edge errors, a six-batch graph budget, no entity resolution across PDFs, and no Neo4j-down fallback (`rag_service.py`, `ingestion_service.py`).

86. **When normal RAG may win.** Fact lookups, exact page questions, PDFs with poor/empty graphs, queries whose wording does not match concept names, and topics where appended prerequisite terms introduce noise. The code already supports vector-ready `READY_WITHOUT_GRAPH`, but runtime still contacts Neo4j (`core/processing.py`, `rag_service.py`).

87. **Actual benefit.** The strongest implemented benefits are conceptual navigation, prerequisite awareness and explainability. Factual grounding is primarily provided by Qdrant passages, thresholds and citations; the evaluation does not isolate GraphRAG versus vector-only retrieval (`evaluation/`, `scripts/run_evaluation.py`).

88. **Claims to avoid.** Do not equate bounded two-hop traversal with multi-step LLM reasoning. Also do not claim learned graph/vector fusion, community detection, global entity resolution, causal correctness, graph database high availability, measured GraphRAG uplift, BM25/hybrid search, autonomous agents, or a knowledge graph built from every PDF chunk. The corresponding mechanisms are absent from `rag_service.py` and `ingestion_service.py`.

## 7. Retrieval and reranking

89. **Pipeline.** READY course resolution → Neo4j term query/bounded two-hop context → separately labelled prerequisite expansion → filtered Qdrant dense top 10 → Cohere/local rerank → 70/30 evidence score and threshold → dedupe/max five citation sources → LLM synthesis (`query.py`, `course_service.py`, `rag_service.py`, `rerank_service.py`, `citation_service.py`).

90. **Initial Qdrant output.** Up to 10 points containing cosine `score`, full chunk `text`, and payload metadata; vectors are not returned (`RetrievalService.search_qdrant`).

91. **Query rewriting.** **Partial.** `_build_expanded_query` appends “Relevant prerequisite concepts: ...” to the original question. There is no LLM paraphrase, decomposition or typo correction (`rag_service.py`).

92. **Query classification.** **Not implemented.** All questions use the same flow (`query.py`).

93. **Prerequisite expansion.** **Both modes are implemented in one bounded retrieval:** direct prerequisites are tagged as hop 1, their prerequisites as hop 2, and each list is labelled separately in vector query expansion. If no graph anchor matches, expansion is skipped (`rag_service.py`).

94. **Multi-query retrieval.** **Not implemented.** One expanded string produces one embedding/search call.

95. **Reranking.** **Implemented.** `RerankService.rerank` uses Cohere in production or an optional local cross-encoder (`rerank_service.py`).

96. **Reranker.** Render config uses Cohere `rerank-v3.5` through direct `httpx` POST to `/v2/rerank`. Local mode lazily loads `cross-encoder/ms-marco-MiniLM-L6-v2` (`rerank_service.py`, `render.yaml`).

97. **Candidates into reranker.** Normally all 10 Qdrant candidates; fewer if Qdrant returns fewer. Cohere `top_n` equals `len(chunks)` (`rag_service.py`, `rerank_service.py`).

98. **Chunks after reranking.** Rerank retains all candidates in rank order, then `assess_evidence` drops those below 0.35. `build_sources` deduplicates and caps final prompt/API sources at five (`rerank_service.py`, `citation_service.py`).

99. **Ranking signals.** Cohere/local cross-encoder relevance is converted to a sigmoid probability and weighted 70%; clamped Qdrant cosine score contributes 30%. This hand-tuned score is not learned or calibrated on the evaluation set (`citation_service.py`).

100. **Duplicate chunks.** `build_sources` deduplicates by `(upload_id, page_number, first 240 normalized passage characters)`. Qdrant candidates themselves are not deduplicated before Cohere, so reranker cost can include near-duplicates (`citation_service.py`).

101. **Context assembly.** Up to five sources each include a ≤900-character normalized passage, filename, page and section. Graph context is compact JSON truncated to 6,000 characters; source labels are enumerated `[Source 1]...` (`citation_service.py`, `SynthesisService._user_prompt`).

102. **Budget.** There is a character budget for graph (6,000), source count (5), source length (900 each), top-k (10), and provider timeouts. There is **no tokenizer-based total token budget** (`synthesis_service.py`, `citation_service.py`).

103. **Too much context.** Extra reranked passages are omitted after five, passages are sliced to 900 characters, and graph JSON is sliced at 6,000 characters. The graph slice can end mid-JSON because it is prompt text, not parsed output (`synthesis_service.py`).

## 8. LLM and generation

104. **Providers/models.** Production primary is Groq `openai/gpt-oss-20b`, optional failover Cerebras `gpt-oss-120b`; optional direct Gemini defaults to `gemini-1.5-flash` but its dependency is separate and not installed by `Dockerfile.api` (`core/config.py`, `render.yaml`, requirements files).

105. **Client initialization.** Groq clients are created per request in `IngestionService`, `SynthesisService`, and `ExamService`; Cerebras uses a small httpx wrapper singleton in `cerebras_service.py`; Gemini config/model is created per call. Qdrant and Neo4j clients are process globals in `core/database.py`.

106. **System prompts.** Three main prompts exist: graph schema extraction in `IngestionService._extraction_system_prompt`, grounded Q&A in `SynthesisService._system_prompt`, and strict exam JSON in `ExamService._system_prompt`. An LLM Cypher prompt exists in `RetrievalService._cypher_system_prompt` but is **not used by the live query path**.

107. **QA instructions.** The Q&A system prompt says use only supplied chunks/graph, no outside knowledge, cite only readable source labels, hide internal IDs/paths/scores, and emit an exact refusal sentence if insufficient (`synthesis_service.py`).

108. **Grounding controls.** PostgreSQL READY scoping, Qdrant upload filters, reranking, evidence thresholds, server-created source cards, bounded prompts, temperature zero, refusal before LLM when no source, and an evidence-only provider fallback are implemented (`query.py`, retrieval/generation services).

109. **Citations.** **Implemented with a caveat.** The backend returns authoritative source cards with pages and preview URLs; the LLM is instructed to place `[Source N]` in answer prose. The backend does not validate that every answer citation exists or force a citation into every supported answer (`citation_service.py`, `synthesis_service.py`). Baseline answers contained inline citations for only 6/10 supported questions (`evaluation/baseline-2026-08-30.json`).

110. **Citation mapping.** `build_sources` creates ordered `source-1...` metadata objects, while `_user_prompt` independently enumerates that list as human labels `[Source 1]...`; ordering maps prose labels to returned cards (`citation_service.py`, `synthesis_service.py`).

111. **Who creates citation IDs.** The backend controls the available numbered labels and source cards; the model chooses whether and where to reproduce labels in answer text. Exam `citation_ids` are model-produced but must match a server-built map or the question is dropped (`exam_service.py`).

112. **Hallucination protections.** The mechanisms in Q108 plus graph-schema/Pydantic validation reduce risk. They do not prove factual entailment, sanitize malicious PDF instructions, or verify generated answer claims sentence-by-sentence.

113. **LLM failure.** For Q&A under Groq/Cerebras transient failure, synthesis returns up to two retrieved passages with citations instead of failing. Graph extraction preserves successful batches and can mark partial/empty graph. Exams return 503 if no provider can produce a valid full exam (`synthesis_service.py`, `ingestion_service.py`, `exam_service.py`).

114. **Retry/fallback.** Graph extraction retries a Groq/Cerebras schema failure once with smaller context/JSON-object mode. Groq transient/schema failure can fail over to Cerebras and a shared five-minute circuit breaker avoids repeated calls. Document-level manual/recovery retry is separate (`ingestion_service.py`, `provider_failover.py`, `upload_service.py`).

115. **Multi-model fallback.** **Implemented for Groq-primary mode** across graph extraction, answer synthesis and exams (`_extract_with_failover`, `_synthesize_with_failover`, `_generate_with_failover`). It is two-provider fallback, not dynamic model routing. Direct Gemini mode and direct Cerebras mode do not fall back to Groq.

116. **If extending fallback.** Add a provider protocol/registry behind the three generation services and centralize error normalization/circuit-breaking. Today failover logic is duplicated in `ingestion_service.py`, `synthesis_service.py`, and `exam_service.py`.

117. **Generation parameters.** Temperature is zero. Graph calls cap 1,000 completion tokens; Cerebras synthesis 1,200; Cerebras exam 2,500; Groq synthesis and Groq exam do not set an explicit completion-token maximum. Provider timeout defaults to 30 seconds (`core/config.py` and generation services).

## 9. FastAPI backend

118. **Endpoints.** `auth.py`: POST/GET/DELETE `/api/v1/auth/session`. `ingest.py`: POST `/ingest/upload`, GET `/ingest/status/{task_id}`, GET `/ingest/uploads`, GET `/ingest/courses`, POST `/ingest/uploads/{upload_id}/retry`, GET preview, DELETE upload. `query.py`: POST `/api/v1/query`. `exam.py`: POST `/api/v1/exam/generate`. `public_demo.py`: GET `/public/sample` and sample preview. `main.py`: GET `/api/v1/health` and `/api/v1/ready`.

119. **Major endpoint data flows.** Upload accepts multipart and touches object storage/PostgreSQL then background Qdrant/Neo4j/LLM. Status/list/courses read PostgreSQL. Retry updates PostgreSQL, checks object storage and enqueues. Preview reads PostgreSQL/object storage. Delete touches PostgreSQL, Qdrant, Neo4j and object storage. Query reads PostgreSQL/Neo4j/Qdrant, calls Cohere and an LLM. Exam reads PostgreSQL/Qdrant and an LLM. Auth uses no DB; it signs/verifies a cookie. Public sample reads PostgreSQL/Neo4j and optionally object storage, with no LLM (`app/api/endpoints/*`).

120. **Project structure.** `main.py` owns lifespan/middleware/routers/health; `api/endpoints` owns HTTP translation; `schemas` owns Pydantic contracts; `models` owns SQLAlchemy tables; `core` owns configuration, clients, processing enums/coordinator/security; `services` contains business/integration logic. There is no separate repository package—`UploadService`/`CourseService` combine repository and domain operations.

121. **Router/service/repository split.** Routers validate HTTP and map exceptions/status codes. Services perform workflows and provider logic. SQL statements belong mostly to `UploadService`/`CourseService`, though `ingest.py` directly takes an advisory lock and `scripts/run_evaluation.py` uses SQL; this is a pragmatic but imperfect separation.

122. **Dependency injection.** FastAPI `Depends` injects async PostgreSQL sessions and cached retrieval/rerank/synthesis/exam services (`core/database.py`, endpoint modules). Many modules also import global clients/singletons, so DI is partial; constructors allow test doubles.

123. **Pydantic.** Request/response schemas validate auth, ingest, query, graph extraction and exams (`app/schemas/*`, endpoint-local models). `GraphExtractionResponse` and exam validators enforce important cross-field integrity.

124. **Async endpoints.** All route handlers are `async def`. Database and Neo4j calls are truly async; blocking SDK work is explicitly sent to threads. `UploadFile.read` is awaited.

125. **I/O-bound operations.** PostgreSQL queries, S3/R2 object calls, Qdrant network calls/inference, Neo4j Cypher, Cohere/Groq/Cerebras/Gemini calls, and PDF preview downloads dominate. Local PyMuPDF/model work is CPU/memory bound but is also offloaded from the event loop.

126. **Connection pools.** SQLAlchemy async engine uses `pool_size=3`, `max_overflow=2`, `pool_pre_ping=True` in production defaults (`core/database.py`, `render.yaml`). Neo4j and Qdrant use their SDK defaults; httpx Cohere clients are normally created/closed per rerank call, so connection reuse is limited.

127. **Error handling.** Endpoints map known course/LLM/storage conditions to 4xx/503 and hide raw provider errors. Processing classifies exceptions into safe persisted categories. Logging uses `logger.exception`. Handling is local per endpoint/service rather than a unified error envelope.

128. **Custom exceptions.** `core/exceptions.py` defines LLM configuration, structure, rate-limit, unavailable and request errors; storage/course/superseded errors exist in their service modules. They are not a comprehensive domain hierarchy.

129. **Global exception handler.** **Not implemented.** FastAPI defaults handle unanticipated exceptions; endpoint try/except blocks do most translation (`main.py`, endpoint modules).

130. **`/health`.** It executes PostgreSQL `SELECT 1` and returns healthy or 503 (`main.py`). It is closer to a database-backed health check than pure process liveness.

131. **Dependency health.** `/health` checks only PostgreSQL. `/ready` checks PostgreSQL, Qdrant collections, Neo4j connectivity, object storage and coordinator started state and reports queue depth (`main.py`). It does not call LLM/Cohere providers.

## 10. PostgreSQL

132. **Tables.** `Course`, `DocumentUpload`, and `ProcessingAttempt` map to `courses`, `document_uploads`, and `processing_attempts` in `app/models/document_upload.py`.

133. **Relationships.** `DocumentUpload.course_uuid` references `Course.id`; `ProcessingAttempt.document_id` references `DocumentUpload.upload_id` with `ON DELETE CASCADE`. SQLAlchemy models do not define ORM `relationship()` attributes; services query explicitly (`models/document_upload.py`).

134. **Canonical course table.** `courses` contains deterministic ID, unique normalized name, display name and creation time. `CourseService.get_or_create/resolve` is the canonicalization boundary (`course_service.py`).

135. **Why immutable UUIDs.** Display spelling/case can change or collide, while UUIDv5 from `conceptgraph:course:{normalized}` gives stable storage/graph identity. Legacy `course_id` display strings remain for compatibility (`course_service.py`, `core/database.py`).

136. **Document association.** `DocumentUpload.course_uuid` is the FK; its legacy `course_id` contains display name. Qdrant metadata calls the canonical course UUID `document_id`, an unfortunate historical name (`models/document_upload.py`, `parser_service.py`).

137. **Processing status storage.** Each document stores coarse `status`, exact `stage`, failure category/retry flag/count, heartbeats/lease, output counts/graph status/result JSON, timestamps and current task ID. Each execution also has a `ProcessingAttempt` history row (`models/document_upload.py`).

138. **Uniqueness.** Primary keys: course ID, upload ID, attempt ID. Unique: `Course.normalized_name`, `DocumentUpload.task_id`, `ProcessingAttempt.task_id`, and `(document_id, attempt_number)`. There is no unique content-hash/course constraint (`models/document_upload.py`).

139. **Indexes.** Indexed fields include normalized course name; document task ID, course display ID, course UUID, content hash, status, stage, graph status, lease expiry; attempt document/task IDs. Some are declared with `index=True`, and compatibility migrations create key document indexes (`models/document_upload.py`, `core/database.py`).

140. **Transactions.** Upload admission holds a transaction advisory lock through dedupe/limit/object-write and `create_upload` commit. Worker stage changes each commit separately; claim/retry/deletion row-lock transactions serialize record mutation. Schema initialization uses `engine.begin()` (`ingest.py`, `upload_service.py`, `core/database.py`).

141. **Application-level consistency.** PostgreSQL cannot transact with Qdrant, Neo4j or S3. READY verification, task fencing, content-addressed keys, cleanup-before-retry and ordered deletion provide application-level consistency (`document_processing_service.py`, `ingestion_service.py`, `ingest.py`).

142. **Deletion coordination.** A row lock keeps the record stable while external derived data and source object are removed; only then does `UploadService.delete_document` delete the PostgreSQL row and possibly orphan course (`ingest.py`, `upload_service.py`). This is ordered compensation, not a distributed transaction.

## 11. Deletion and cleanup

143. **READY deletion trace.** `DELETE /api/v1/ingest/uploads/{upload_id}` verifies READY/FAILED, row-locks it, calls `cleanup_upload` for Qdrant/Neo4j, checks whether the content-addressed object is shared, deletes it if unshared, then deletes the document and possibly course in PostgreSQL (`ingest.py`).

144. **PostgreSQL deletion.** `DocumentUpload` is removed; its `ProcessingAttempt` rows cascade; `Course` is removed only when no documents remain (`UploadService.delete_document`).

145. **Qdrant deletion.** All points with matching payload `upload_id` are deleted with `wait=True` (`IngestionService.cleanup_upload`).

146. **Neo4j deletion.** All Concept nodes under the target Course with matching upload ID are `DETACH DELETE`d; an empty Course node is then deleted (`IngestionService.cleanup_upload`).

147. **Object deletion.** `StorageService.delete` removes the local file or calls S3 `delete_object`; it is skipped if another row references the same storage key (`ingest.py`, `storage_service.py`).

148. **Order.** Qdrant → Neo4j → unshared object storage → PostgreSQL document/attempt/course. Qdrant and Neo4j share one `cleanup_upload` call (`ingestion_service.py`, `ingest.py`).

149. **Step failure.** Derived cleanup failure rolls back the PostgreSQL session and leaves source/row. Object deletion failure also keeps the PostgreSQL row. However, earlier external steps are not rolled back, so a retry is needed to converge (`ingest.py`).

150. **Cross-store transactionality.** **Not implemented** and impossible with the chosen independent services without a saga/outbox design.

151. **Partial cleanup handling.** Operations are intended to be idempotent: deleting missing Qdrant points/nodes/objects can be repeated. The user receives 503 and can retry. There is no durable deletion tombstone, outbox or automated reconciliation for every partial deletion (`ingest.py`, `ingestion_service.py`).

152. **Why READY deletion.** It frees the installation-wide 50-document capacity and implements a complete data lifecycle instead of leaving vectors/graphs/PDFs permanently. `UploadService.DELETABLE_STAGES` includes READY and FAILED.

153. **Automatic cleanup.** **Implemented for protected demo deployments.** `DemoRetentionService` periodically deletes eligible READY/FAILED uploads from every store. It is disabled when `REQUIRE_UPLOAD_AUTH=false` (`demo_retention_service.py`).

154. **Retention.** Default and Render value are three days; the configured public sample course is excluded (`core/config.py`, `render.yaml`, `demo_retention_service.py`).

155. **Scheduling.** FastAPI lifespan starts an in-process loop; it runs immediately, then every 21,600 seconds/six hours on Render (`main.py`, `demo_retention_service.py`, `render.yaml`). Multiple API replicas would each run the sweep, relying on row locks/idempotence rather than a singleton scheduler.

## 12. Storage and limits

156. **Provider.** The abstraction supports `s3` and `local`. Production uses S3-compatible Cloudflare R2-style configuration; code uses generic boto3, so the actual vendor is determined by `S3_ENDPOINT_URL` (`storage_service.py`, `render.yaml`).

157. **Upload/download.** The API reads the full multipart file into memory and calls boto3 `put_object`. Processing/preview call `get_object` and read the full body; preview then serves full or HTTP byte-range slices (`ingest.py`, `storage_service.py`). It does not stream directly between client and object store.

158. **Signed URLs.** **Not implemented.** Preview bytes are proxied through authenticated/public-scoped FastAPI routes (`ingest.py`, `public_demo.py`). This enforces access but adds API memory/bandwidth cost.

159. **Per-user limits.** **Not implemented.** `MAX_PDFS_PER_INSTALLATION=50` is global because the app has no users. The UI/backend also cap each PDF at 10 MiB (`core/config.py`, `ingest.py`).

160. **Limit enforcement.** `upload_document` holds the advisory admission lock, calls `UploadService.count_uploads`, and returns 429 at the configured count (`ingest.py`). The count includes READY, active and FAILED records until deleted.

161. **Usage calculation.** It executes `COUNT(document_uploads.upload_id)`; no bytes, tenant, or storage-provider quota is calculated (`UploadService.count_uploads`).

162. **Limit reached.** Upload returns HTTP 429 advising removal of an eligible record. READY/FAILED deletion frees one slot (`ingest.py`).

163. **Trade-off.** A small global cap protects free-tier storage/vector/LLM cost and keeps a shared portfolio demo predictable, but it is not fair among reviewers and can be exhausted by one holder of the shared code.

## 13. Authentication and security

164. **Authentication.** **A shared demo gate is implemented; real user authentication is not.** `DemoProtectionMiddleware`, `auth.py`, `security_service.py`, and `DemoAccessGate.tsx` implement it.

165. **Mechanism.** A user exchanges one server-side access code for an HMAC-SHA256 signed timestamp cookie. The cookie is HttpOnly, configurable Secure/SameSite and expires after 12 hours on Render. A bearer equal to the same access code also works (`DemoAccessService`, `auth.py`).

166. **Identity propagation.** There is no user identity. Middleware only verifies possession of the shared secret/cookie and does not attach a principal to request state (`core/security.py`).

167. **Preventing cross-user access.** **Not implemented.** All authenticated reviewers see the same uploads/courses/PDFs; only the public sample routes are restricted to the configured sample course (`DemoAccessGate.tsx`, `public_demo.py`).

168. **Authorization location.** Route-level protection is centralized in middleware. Public sample preview performs explicit membership authorization. Other services do not check ownership because no ownership model exists (`core/security.py`, `public_demo.py`).

169. **Secrets.** PostgreSQL, Qdrant, Neo4j, S3/R2, Groq, Cerebras, Cohere, optional Gemini, demo access token and allowed origins are environment settings (`core/config.py`, `.env.example`, `render.yaml`). Only selected values use Pydantic `SecretStr`; all must remain server-side.

170. **Secret loading.** `pydantic-settings` reads environment variables and local `.env`; Render marks secrets `sync:false`. Frontend receives only `VITE_API_BASE_URL` (`core/config.py`, `render.yaml`, `src/services/api.ts`).

171. **PDF validation.** Extension, declared MIME, `%PDF-` magic, nonempty body, size, PyMuPDF parse, password flag and page count are checked (`ingest.py`). It is better than MIME-only but not malware scanning or sandboxed parsing.

172. **Security weaknesses.** Shared secret/no revocation per user, no tenant isolation, in-memory/global rate limits, no CSRF token despite cross-site `SameSite=None`, permissive `allow_methods/headers=*` for configured origins, full PDF parsing in-process, no malware scan, no content security policy in `render.yaml`, and API docs are protected only when the demo token is enabled.

173. **Prompt injection.** **Not specifically defended.** Prompts say use only course context, but untrusted PDF text is placed directly into graph/answer/exam prompts (`ingestion_service.py`, `synthesis_service.py`, `exam_service.py`). There is no instruction/data delimiting policy enforcement or output entailment verifier.

174. **Rate limiting.** **Implemented only in-process.** Fixed one-minute counters protect login, expensive and standard routes; public sample uses IP fingerprint. Protected traffic is effectively keyed by the one configured shared token, so all reviewers share a quota. Counters reset on restart and do not coordinate across replicas (`security_service.py`, `core/security.py`).

## 14. Frontend

175. **Framework/libraries.** React 18 + TypeScript + Vite, Tailwind, Cytoscape.js, React Markdown/remark-gfm, Lucide, Motion/Framer Motion and a few small UI utilities (`package.json`).

176. **Structure.** `App.tsx` owns a two-page history-based router; `pages/Home.tsx` and `Dashboard.tsx` are screens; `components/` contains graph, upload, preview, exam, auth and sample features; `services/api.ts` owns typed HTTP calls; `index.css`/Tailwind own styling.

177. **Backend calls.** `api.ts` derives `API_BASE_URL` from `VITE_API_BASE_URL`, sends cookies with `credentials: include`, applies AbortController timeouts, parses FastAPI error details and exposes typed functions. There is no React Query/SWR cache.

178. **Loading/errors.** Components use local `useState`; API timeout/network errors become readable `Error`s. Dashboard shows spinners/errors, each upload can retain a polling error, and `AppErrorBoundary` catches render failures. There is no toast framework or centralized retry policy (`api.ts`, `Dashboard.tsx`, `AppErrorBoundary.tsx`).

179. **Processing status.** Dashboard fetches uploads/courses, polls active task endpoints every 2.5 seconds with `Promise.allSettled`, shows stage/status/error/attempt/graph coverage, and auto-selects a newly READY course (`Dashboard.tsx`).

180. **Graph fetch/display.** A Q&A response returns `graph_context`; `buildGraphElements` converts it to deduplicated nodes/edges; lazy-loaded `ConceptGraphCanvas` creates a Cytoscape COSE layout (`Dashboard.tsx`, `ConceptGraphCanvas.tsx`). Before a question, the protected dashboard does not fetch a full selected-course graph; the public sample endpoint does.

181. **Why Cytoscape.** It supplies graph-native nodes/edges, directional arrows, layout, pan/zoom/drag, event handlers and fit operations without implementing a renderer (`ConceptGraphCanvas.tsx`).

182. **Graph interactions.** Users can pan, zoom, drag, fit, select/dim, animate backward along displayed prerequisite edges, view concept description/type/provenance and open the source page (`ConceptGraphCanvas.tsx`).

183. **Fit to view.** `cy.fit(undefined, 48)` adjusts zoom/pan so all current elements fit the viewport with padding; it runs after data/layout and from the maximize button (`ConceptGraphCanvas.tsx`).

184. **Concept details.** Node tap reads Cytoscape data into `selectedNode` and shows an overlay with name, type, description, PDF, page and section (`ConceptGraphCanvas.tsx`).

185. **PDF preview.** `PdfPreviewModal` embeds the authenticated/public API preview URL and offers an external open link. Backend supports Range requests and `#page=N` browser navigation (`PdfPreviewModal.tsx`, `ingest.py`).

186. **Lazy routes/components.** Home and Dashboard are `React.lazy` in `App.tsx`; graph and exam components are lazy in `Dashboard.tsx`; public sample also lazy-loads the graph (`PublicSampleCourse.tsx`).

187. **Bundle optimization.** Route and heavy graph/exam code splitting are implemented through `lazy/Suspense`; Vite tree-shakes production code. Do not claim an exact historical bundle reduction without a like-for-like archived build report (`App.tsx`, `Dashboard.tsx`, `vite.config.ts`).

188. **Error boundary.** `AppErrorBoundary.tsx` is a class error boundary around routed content; it logs and provides reload/back actions. It catches render/lifecycle errors, not arbitrary async promise failures.

189. **Performance improvements.** Lazy loading, bounded displayed graph data, memoized graph-element conversion, API timeouts and concurrent upload/course fetching are present. Remaining issues include 2.5-second polling, full PDF proxying, re-creating the COSE layout, no request/cache library, and rebuilding all graph elements on each response (`Dashboard.tsx`, `ConceptGraphCanvas.tsx`, `api.ts`).

## 15. Practice exam and secondary features

190. **Practice exam.** **Implemented.** `POST /api/v1/exam/generate`, `ExamService`, schemas in `schemas/exam.py`, and `ExamPanel.tsx` provide 1–20 question MCQ generation.

191. **Flow.** Endpoint READY-gates course → `ExamService` scrolls all matching Qdrant payloads → balances up to 12 sources by document/section/page → builds bounded passages → calls selected LLM/failover → validates JSON, options and citation IDs → requires exactly requested valid question count → returns coverage (`exam.py`, `exam_service.py`).

192. **Model generation.** The system prompt requires exactly N questions, four options, exact matching correct answer, explanation/topic and citation IDs from provided labels; `_parse_questions` converts only valid results to `MockQuestion` (`exam_service.py`, `schemas/exam.py`).

193. **Grounding.** Sources come only from READY document upload IDs and each accepted question must cite at least one supplied source ID. However, the backend validates citation identity, not whether the question/explanation is semantically entailed by the passage (`exam_service.py`).

194. **Difficulty/topics.** The user cannot select difficulty. The LLM invents a concise `topic` from balanced context and is asked to spread questions across topics/documents. Returned coverage summarizes observed topics/documents/pages (`exam_service.py`).

195. **Other features.** Public read-only sample, answer confidence, evidence cards, graph-quality/coverage states, provider quota messages, retry history, automatic three-day retention, complete deletion, byte-range PDF preview, evaluation scripts and CI are interview-relevant (`public_demo.py`, `citation_service.py`, `demo_retention_service.py`, `evaluation/`, `.github/workflows/`).

## 16. Reliability and production engineering

196. **Implemented mechanisms.** Strict startup configuration; PostgreSQL source of truth; admission advisory lock; SHA-256/content-addressed objects; bounded durable dispatch; row claims, leases, heartbeats and task-token fencing; processing-attempt history; explicit stages; READY gating; deterministic Qdrant IDs; upload/execution provenance; cleanup before retry/after failure; manual and stale recovery; graph validation/partial states; timeouts; Groq→Cerebras failover/cooldown; evidence fallback/refusal; cross-store deletion; retention; readiness probes; safe errors/logs; rate limits; CI and focused tests. Evidence is spread across `core/`, `services/`, `main.py`, tests and `render.yaml`.

197. **Retries.** Document retries create a new attempt/task ID and replay from source after cleanup, capped at three total. Stale attempts recover automatically after lease expiry; users manually retry ordinary transient FAILED records. Provider schema fallback and two-provider failover are intra-attempt retries (`upload_service.py`, `processing_coordinator.py`, `ingestion_service.py`).

198. **Idempotency.** Content-addressed storage makes repeat writes target the same key; deterministic Qdrant point IDs make upserts overwrite; Neo4j uses `MERGE`; every processing attempt starts with upload-scoped cleanup; task IDs and execution tokens identify the current attempt (`storage_service.py`, `ingestion_service.py`, `document_processing_service.py`). It is best-effort cross-store idempotency, not exactly-once delivery.

199. **Duplicate protection.** SHA-256 + canonical course lookup under a transaction advisory lock handles concurrent endpoint uploads. Lack of a unique DB constraint remains the weak point (`ingest.py`, `UploadService.find_duplicate`).

200. **READY gating.** `mark_completed` enforces prerequisites at write time; `get_ready_context` enforces stage and positive chunks at read time. Graph completeness is represented separately, allowing vector-only READY (`upload_service.py`, `course_service.py`).

201. **Compensating cleanup.** Upload-scoped Qdrant/Neo4j outputs are deleted before attempts and after caught failures; source/PostgreSQL are retained for retry. Manual/retention deletion extends cleanup to source and control rows (`document_processing_service.py`, `ingestion_service.py`, `ingest.py`).

202. **Advisory lock.** `pg_advisory_xact_lock(hashtext('conceptgraph:pdf-admission'))` serializes all upload admission across API processes sharing PostgreSQL. It is simple and correct for a 50-file demo but lowers concurrent admission throughput (`ingest.py`).

203. **Per-attempt resource/client creation.** Fresh SQLAlchemy sessions are created for worker operations/stages, Neo4j sessions are context-managed, and Groq clients are per call. But Qdrant and Neo4j drivers are global, storage client is lazy singleton, and Cerebras/Cohere client behavior differs. Therefore “all clients are recreated per attempt” would be false (`core/database.py`, `document_processing_service.py`, provider services).

204. **Permanent/retryable errors.** `classify_failure` maps document/configuration/missing-source failures to permanent and provider/connectivity/worker failures to retryable; `mark_failed` disables retry at attempt three (`core/processing.py`, `upload_service.py`). String matching can misclassify novel errors.

205. **Dependency failures.** PostgreSQL down blocks startup/health/all state operations. Qdrant down fails ingestion or query/exam and normally yields retryable FAILED/503. Neo4j down fails ingestion and query; no vector-only runtime fallback. Object storage down blocks admission, processing, preview/deletion and readiness. LLM down leads to partial/empty graph, Q&A evidence fallback, and exam 503; configuration errors can prevent processing. These paths are in `main.py`, endpoint handlers and processing/generation services.

206. **Observability.** Stage/failure/retry/heartbeat/results are persisted and shown in Dashboard; Python logs record exceptions/provider fallback; `/health` and `/ready` expose health. **No metrics, traces, request IDs, alerting, structured logging or centralized log dashboard are configured in the repository.**

207. **Remaining production risks.** In-process queue/scheduler, multi-replica cleanup races, nontransactional external writes/deletion, no outbox/dead-letter queue, brittle string error classification, shared auth, memory rate limiter, no OCR/malware sandbox, prompt injection, whole-file memory use, no Neo4j fallback, provider/free-tier limits, no live integration/load tests, and no secrets rotation workflow.

## 17. Performance and scaling

208. **Likely bottlenecks.** One processing worker, up to six sequential graph LLM batches, full-document embedding upsert, external provider latency/quotas, Neo4j-before-every-query, Cohere and LLM network calls, small PostgreSQL pool, full PDF memory proxying, and client polling (`render.yaml`, processing/retrieval services, `Dashboard.tsx`).

209. **Most expensive operations.** LLM graph extraction and answer/exam generation dominate latency/cost; hosted embeddings and reranking add calls. Large PDF parsing/embedding and graph layout are CPU/memory concerns (`ingestion_service.py`, generation services, `ConceptGraphCanvas.tsx`).

210. **100 simultaneous uploads.** Admission is serialized; each file is fully read; every accepted row is durable. Only eight jobs fit the in-memory queue and one worker processes at a time; others receive 202 as deferred and dispatcher sweeps them later. The global 50-record cap means a fresh installation rejects at least half (`ingest.py`, `processing_coordinator.py`, `render.yaml`).

211. **10,000 simultaneous questions.** There is no capacity for this: each request calls PostgreSQL, Neo4j, Qdrant, Cohere and an LLM; in-memory shared rate limiting and provider quotas intervene, the DB pool is tiny, and there is no cache/backpressure queue for queries. Expect 429s, timeouts and 503s.

212. **Job queue.** A bounded `asyncio.Queue` exists inside `ProcessingCoordinator`; there is **no durable external queue** such as Celery/Redis/SQS. PostgreSQL UPLOADED rows act as the recoverable backlog (`processing_coordinator.py`, `upload_service.py`).

213. **Background work.** FastAPI lifespan owns coordinator workers/dispatcher and the retention loop (`main.py`). If the process sleeps/stops, work pauses until startup recovery.

214. **Horizontal scaling.** Stateless request handlers and external data stores can scale in principle. Worker claims/leases help coordinate replicas, but each replica has its own queue, rate limiter, circuit breaker and retention loop; these mechanisms are not globally coordinated (`processing_coordinator.py`, `security_service.py`, `provider_failover.py`).

215. **Local application state.** Durable data is external in production, but queue membership, limiter counters, circuit-breaker cooldowns and cached service/model objects are process-local. Local development may store PDFs under `data/uploads` (`core/processing_coordinator.py`, provider/security/storage services).

216. **Caching.** Only Python `lru_cache` for settings and endpoint service instances exists. There is no answer, embedding, query-result, graph, HTTP or distributed application cache (`core/config.py`, `query.py`, `exam.py`).

217. **Cache candidates.** Query embeddings by normalized question, course graph/totals keyed by READY upload set, retrieval/rerank results, public sample payload, identical grounded answers, and PDF proxy ranges. Any cache must invalidate on READY/deletion and must preserve future tenant boundaries.

218. **Critical indexes.** PostgreSQL task ID, course UUID+stage, lease expiry, content hash and attempt task/document indexes drive status, READY resolution, dispatch/dedup and history. Qdrant's upload-ID payload index drives filters (`models/document_upload.py`, `core/database.py`, `ingestion_service.py`). A composite `(course_uuid, content_hash)` unique index and `(status, stage, lease_expires_at)` dispatch index are missing improvements.

219. **Frontend fixes.** Route/graph/exam lazy loading, memoized graph conversion, concurrent list requests, bounded graph rendering and collapsed processing details reduce initial/visual load (`App.tsx`, `Dashboard.tsx`). Exact bundle-reduction claims are not reproducibly stored.

220. **First 10× changes.** Move ingestion to a durable external queue/worker pool; add distributed rate limiting and provider concurrency controls; make Neo4j degradation vector-only; stream/presign PDFs; add composite DB indexes; cache stable graph/retrieval work; add observability/load tests. Keep per-course READY filters and attempt idempotency.

## 18. Testing

221. **Tests present.** `tests/test_processing.py` covers processing, providers, validation, auth, Qdrant, graph, citations, storage, endpoints, deletion, retention, fencing, READY gating, coordinator and recovery. `tests/test_evaluation.py` tests evaluation-data/scoring rules. At audit time the suite contains 100 unittest cases.

222. **Unit tests.** Most tests are unit/service-contract tests with `unittest`, `AsyncMock`, fake clients/sessions and patched settings. Strong examples include concept normalization, graph status, evidence scoring, provider error normalization and parser behavior (`tests/test_processing.py`).

223. **Integration tests.** Endpoint functions and multi-service workflows are tested with fakes, but there is no container-backed test suite that starts real PostgreSQL/Qdrant/Neo4j/R2/providers/browser. Therefore “100 integration tests” would be inaccurate.

224. **Ingestion failures.** Yes: provider timeout/quota/JSON, no-text scan, Qdrant DNS, worker interruption, cleanup and processing partial-output failures are covered (`tests/test_processing.py`).

225. **Retries.** Yes: retry cap, deferred full queue, stale recovery attempts, leases and stale task fencing are covered.

226. **Duplicate uploads.** Partial: course normalization, object sharing and endpoint storage order are tested, but there is no real concurrent PostgreSQL test proving advisory-lock dedupe under two transactions.

227. **Deletion across stores.** Yes with mocked stores: failed/READY deletion, active rejection, shared object retention, cleanup failure, orphan/retained course and retention deletion are tested.

228. **GraphRAG retrieval.** Key behaviors—empty graph evidence, direction, query filtering, provenance and citations—are tested, but there is no comparative relevance test showing graph expansion beats vector-only retrieval.

229. **LLM evaluation.** A manually annotated 15-question dataset and executable evaluator measure document/page top-five, inline citation, refusal and latency (`evaluation/questions.json`, `scripts/run_evaluation.py`). Stored baseline: 8/10 document, 8/10 page, 6/10 inline citations, 5/5 unsupported refusals, 7.584 s average. It is a small baseline, not statistical model evaluation.

230. **Critical missing tests.** Real service integration, concurrent admission/deletion, crash during each external write, multi-replica leases, browser E2E/accessibility/mobile, prompt injection/malicious PDFs, large files/memory, load/rate coordination, object/DB ambiguous commits, GraphRAG ablation, and live provider contract tests.

## 19. Deployment

231. **Backend.** Render Blueprint deploys `conceptgraph-api` as a free Docker web service using `Dockerfile.api`, `/api/v1/ready` health checks and external managed credentials (`render.yaml`).

232. **Frontend.** Render deploys `conceptgraph-frontend` as a static site using `npm ci && npm run build`, publishes `dist`, injects `VITE_API_BASE_URL`, sets security headers and rewrites routes to `index.html` (`render.yaml`).

233. **Docker.** Backend and frontend Dockerfiles exist; `docker-compose.yml` runs local PostgreSQL 16, Qdrant, Neo4j 5 Community and MinIO. Render's frontend uses native static build rather than `Dockerfile.frontend`.

234. **Dockerfile behavior.** `Dockerfile.api` installs pinned Python dependencies in slim Python 3.12, creates a non-root user and launches Uvicorn. `Dockerfile.frontend` uses Node 20 multi-stage build then unprivileged nginx with a health check.

235. **Environment configurations.** `.env.example` and config defaults support local services/local models; `docker-compose.yml` supplies local databases/storage; `render.yaml` selects hosted embeddings/reranking, strict startup validation, secure cross-site cookie, S3 and external stores (`core/config.py`).

236. **Required cloud services.** Render frontend/API, PostgreSQL, Qdrant, Neo4j, S3-compatible object storage, Groq, Cohere, and optionally Cerebras. The repository does not provision those third-party databases/buckets via infrastructure as code.

237. **Interview deployment explanation.** “The React app is a Render static site; FastAPI is a non-root Docker service. Stateful data is externalized to PostgreSQL, Qdrant, Neo4j and R2. A Render Blueprint injects only public frontend URL at build time and server secrets at runtime. Readiness verifies the four required data stores before routing traffic.” (`render.yaml`, Dockerfiles, `main.py`).

238. **Free-tier dependence.** Render plan is explicitly free; docs/config are optimized around hosted/free-tier Groq/Cerebras/Cohere/Qdrant/Neo4j/R2 limits. Exact third-party plan availability is external and can change; the code responds with partial graphs, fallback evidence, caps, rate limits and retention (`render.yaml`, README, provider services).

## 20. Interview weakness check

239. **Challengeable résumé claims.** “SentenceTransformer embeddings” needs the qualifier “MiniLM via Qdrant Cloud in production; local optional.” “Cross-encoder reranking” should say “Cohere rerank in production; local cross-encoder optional.” “GraphRAG” should be described as bounded one-hop/two-hop prerequisite query expansion, not autonomous multi-step reasoning. “Authentication” is one shared code, not accounts. “Async jobs” are in-process, not Celery. “Hybrid search,” OCR, multi-tenancy, measured graph uplift and an exact 927→150 KB optimization are unsupported.

240. **Code support for safe claims.** Page-aware chunking: `parser_service.py`; Qdrant vectors: `ingestion_service.py`; bounded one-hop/two-hop graph expansion: `rag_service.py`; citations/evidence: `citation_service.py`/`synthesis_service.py`; APIs: `api/endpoints`; durable attempts: `models/document_upload.py`/`upload_service.py`; deletion: `ingest.py`; provider failover: three generation services; dashboard: `src/`; evaluation: `evaluation/` and `scripts/run_evaluation.py`; tests: `tests/`; deployment: `render.yaml` and Dockerfiles.

241. **Impressive but partial.** Graph quality uses coverage/status but no semantic quality metric. Failover handles two providers but routing is duplicated and process-local. Durable background processing uses PostgreSQL leases but the queue itself is not external. Citations have authoritative cards but inline LLM citations are not enforced. Authentication protects cost but provides no identity/isolation. Evaluation has only 15 questions from one course.

242. **Barely/conditionally used technologies.** Gemini is optional and absent from production dependencies. Local SentenceTransformers/PyTorch are optional and absent from API Docker. `generate_cypher` and its LLM prompt exist but live retrieval always calls `_fallback_cypher`. Motion/UI packages appear mainly on the landing page, not core architecture. `week_number` is retained only for schema compatibility (`requirements-*`, `rag_service.py`, `upload_service.py`).

243. **Criticizable decisions.** Four specialized data stores/services for a student app; global admission lock; in-process work/rate limits; full PDF buffering; no unique dedupe constraint; no Neo4j-down fallback; substring graph matching; broad graph fallback; fixed thresholds; no tenant auth; schema changes through runtime `ALTER TABLE`; and no migrations framework.

244. **Justifications and honest trade-offs.** PostgreSQL gives durable orchestration but adds state complexity; Qdrant specializes semantic search but adds another dependency; Neo4j makes direction/provenance and bounded prerequisite traversal explainable but may still be operationally heavy for this project size; R2 keeps binaries out of DB but breaks atomicity; in-process workers minimize free-tier deployment complexity but limit scale; hosted inference fits low memory but adds latency/quotas; shared auth makes recruiter demos easy but intentionally is not SaaS security; strict validation limits bad LLM data but can yield partial graphs.

# 21. Final interview output

## A. 60-second explanation

> ConceptGraph is a course-aware GraphRAG portfolio application. A reviewer uploads a PDF, and FastAPI validates it, stores the source in S3-compatible object storage, records durable processing state in PostgreSQL, extracts page-aware 500-word chunks, and indexes 384-dimensional MiniLM embeddings in Qdrant. In parallel, an LLM extracts a validated concept graph with six allowed relationship types and source-page provenance, which is stored in Neo4j. When a student asks a question, the backend resolves only READY documents, retrieves matching concepts plus direct and two-hop foundational prerequisites from Neo4j, uses those separately labelled terms to expand a filtered Qdrant search, reranks ten candidates, rejects weak evidence, and asks Groq—with Cerebras failover—to answer from at most five cited passages. I focused on explainability and failure handling: bounded traversal, explicit graph-quality states, retries with leases and fencing, complete cross-store deletion, provider fallback, and a small measured evaluation baseline.

## B. Two-minute technical explanation

> The React/TypeScript frontend is a Render static site and the FastAPI backend is a Dockerized modular monolith. PostgreSQL is the source of truth for canonical courses, uploads and processing attempts. A PDF admission transaction checks the real bytes, takes a global advisory lock, computes SHA-256 for course-scoped deduplication, writes a content-addressed source object, and persists an UPLOADED job before submitting it to a bounded in-process coordinator.
>
> A worker claims the row using a PostgreSQL row lock and lease. It cleans stale outputs, reads the PDF, extracts text page-by-page with PyMuPDF, creates 500-word chunks with 50-word overlap, and stores vectors plus filename/page/section/upload metadata in one cosine Qdrant collection. Graph extraction samples each section, applies a six-batch free-tier budget, asks the LLM for strict JSON, validates endpoints and six relationship types, rejects invented source IDs, merges concepts by lowercase whitespace-free names, and stores course/upload-scoped Neo4j nodes. An empty graph does not destroy usable vectors; the document is marked GRAPH_READY, GRAPH_PARTIAL or READY_WITHOUT_GRAPH.
>
> At query time, PostgreSQL returns only READY upload IDs. Deterministic Cypher substring-matches up to five concepts and retrieves inbound prerequisite paths to depth two. Direct and foundational prerequisite names are separately labelled before a top-10 Qdrant search filtered by those upload IDs. If no anchor matches, the original vector query is preserved. Cohere reranks the candidates; a 70/30 rerank/vector score filters evidence; up to five deduplicated passages become source cards and the LLM prompt. If Groq is rate-limited or times out, Cerebras is tried and a five-minute process-local cooldown avoids repeated failures. If both fail, the user still receives the strongest retrieved passages. Important limitations are no OCR, no BM25, no true user accounts, no external job queue, and no measured GraphRAG-versus-vector-only uplift.

## C. Five-minute architecture walkthrough

1. **Boundary and security.** `DemoProtectionMiddleware` protects `/api/v1` except health/auth/public sample. `DemoAccessService` exchanges one high-entropy reviewer code for a stateless signed cookie. Explain clearly that this is cost protection, not multi-tenant authentication.
2. **Admission.** `upload_document` validates suffix, MIME, byte limit, PDF magic, parseability, encryption and page count. It computes SHA-256 under a PostgreSQL advisory transaction lock, resolves a deterministic course UUID, detects course-scoped duplicates and enforces the 50-record installation cap.
3. **Durability before work.** The content-addressed object is written before `DocumentUpload` and `ProcessingAttempt` commit. A queue-full result does not lose work: the persisted UPLOADED row is picked up by dispatcher sweeps.
4. **Worker coordination.** `ProcessingCoordinator` has a bounded asyncio queue and one Render worker by default. A worker row-locks/claims an attempt, sets a 180-second lease and runs a 30-second heartbeat. Task ID and lease owner fence stale attempts. Restart recovery creates a new attempt after expiry, up to three total.
5. **Parsing/indexing.** `ParserService` extracts native text, detects simple headings and emits page-bounded 500/50 chunks. `IngestionService` embeds locally or through Qdrant Cloud and upserts deterministic UUIDv5 points into one 384-dimensional cosine collection with an upload-ID payload index.
6. **Graph construction.** Up to nine chunks per section are evenly sampled; batches contain up to four chunks and global extraction defaults to six batches. Groq/Cerebras/Gemini must produce locally validated nodes/edges. Provenance is attached only when a cited source ID exists. Neo4j receives scoped Course/Concept nodes and directional relationships.
7. **Honest readiness.** Source exists + positive vector count + completed graph attempt + valid graph status are required. Useful vectors may be READY even with no validated graph. Failures clean derived outputs and keep the source/control record for retry.
8. **Retrieval.** `CourseService` returns only deduplicated READY uploads. Deterministic read-only Cypher uses term containment and a maximum two-hop prerequisite traversal. Hop-one and hop-two names separately expand one dense Qdrant query; without an anchor, expansion is omitted. Ten results go to Cohere/local reranking and thresholding; five deduplicated passages become sources.
9. **Generation.** The prompt includes bounded graph JSON and labelled passages, requires course-only answers and readable citations, and uses temperature zero. Groq→Cerebras handles transient failure; evidence-only fallback preserves usefulness. Inline citations remain model-controlled and were only 6/10 in the stored baseline.
10. **Lifecycle/deployment.** READY/FAILED deletion removes Qdrant, Neo4j, unshared object and PostgreSQL in order. A six-hour loop removes three-day-old demo uploads but excludes the sample. Render deploys static frontend + Docker API; readiness checks all stores. Close by acknowledging that external stores are not atomically transactional and the worker/limiter/circuit breaker are process-local.

## D/E. Thirty likely questions with concise answers

1. **Why four data stores?** PostgreSQL owns durable control state, Qdrant semantic vectors, Neo4j explicit relationships, and object storage immutable PDF bytes. This gives clear responsibilities but increases operations; for a smaller rebuild I would assess pgvector before keeping Qdrant.
2. **Why is this GraphRAG?** Neo4j concepts/prerequisites participate before vector retrieval by expanding the query and graph JSON is passed to synthesis. It is bounded two-hop graph-assisted RAG, not autonomous multi-step reasoning.
3. **Describe upload durability.** Source object and PostgreSQL UPLOADED/attempt records exist before queue admission. If the queue is full or process restarts, dispatcher recovery finds the durable row.
4. **How do you prevent duplicate PDFs?** SHA-256 over admitted bytes, canonical course UUID and a transaction advisory admission lock. Missing improvement: a unique `(course_uuid, content_hash)` constraint.
5. **Why use an advisory lock?** It serializes dedupe/count/course admission across API instances. It is simple at demo scale but globally reduces upload admission concurrency.
6. **What makes a document READY?** Current active fenced attempt, GRAPH_BUILT stage, canonical course, source key/object, positive complete vector count, and a valid graph-quality status.
7. **Can an empty graph be READY?** Yes: `READY_WITHOUT_GRAPH` preserves valid vector search rather than making LLM graph failure destroy document usefulness.
8. **What happens on a crash?** The heartbeat stops, the lease expires, startup/dispatcher recovery closes the interrupted attempt and creates a new task/attempt, capped at three.
9. **How is stale work fenced?** Every mutation matches upload ID + current task ID + lease owner. External records also carry execution token; stage checks stop superseded attempts.
10. **How do retries avoid duplicates?** Cleanup runs before each execution, Qdrant IDs are deterministic, and Neo4j writes use scoped IDs/MERGE.
11. **How is text chunked?** Native PDF text is kept page-bounded, headings are heuristically detected, and sections are split into 500-word windows with 50-word overlap.
12. **Why Qdrant?** It performs cosine semantic search over MiniLM vectors and filters by READY upload IDs, retrieving related meaning rather than exact terms.
13. **What does each Qdrant point contain?** UUIDv5 point ID, 384-dimensional vector, full chunk text, course UUID, upload/execution/chunk IDs, filename, page, section and chunk index.
14. **How do you isolate courses?** PostgreSQL resolves one course's READY upload UUIDs; Qdrant and Neo4j queries filter those IDs. There is no user-level isolation.
15. **How does Neo4j improve retrieval?** Term-matched concepts expose inbound prerequisite neighbors; their names are appended to the semantic query. No numerical graph/vector score fusion exists.
16. **How many graph hops?** One hop in backend retrieval. The UI can animate longer chains only among edges already returned.
17. **Is Cypher generated by an LLM?** A generator exists, but the live path deliberately uses `_fallback_cypher`, a fixed parameterized read-only query. Claiming live LLM Cypher would be false.
18. **How is bad graph output handled?** Strict schema, Pydantic endpoint/type validation, source-ID validation, local size limits, smaller JSON fallback, provider failover, and partial/empty status.
19. **How are concepts deduplicated?** Case-fold and remove whitespace, keep the first concept, remap edges and aggregate provenance. This is intentionally simpler than entity resolution.
20. **How is retrieval ranked?** Qdrant top 10 → Cohere/local reranker → score = 70% sigmoid reranker + 30% cosine → 0.35 threshold → at most five sources.
21. **Is search hybrid?** No. It is dense vector search with graph-based text expansion; there is no BM25/sparse vector fusion.
22. **How do citations work?** Backend constructs source cards and prompt labels from retrieved metadata; the model writes readable labels. Cards are authoritative, but inline label presence is not enforced.
23. **How do you refuse unsupported questions?** If no reranked passage meets the threshold, the router skips the LLM and returns a fixed insufficient-evidence response.
24. **How does provider failover work?** Groq transient/schema failure triggers optional Cerebras and a process-local five-minute circuit breaker; synthesis can finally return evidence-only passages.
25. **How does deletion work?** Lock row; delete Qdrant points and Neo4j concepts; delete source only if unshared; delete document/attempts and orphan course. It is idempotent but not atomic across stores.
26. **How is auth implemented?** One access code signs a timestamp cookie using HMAC-SHA256. No session database or user identity exists.
27. **How are PDFs protected?** Private previews require the shared session; public preview verifies the upload belongs to the configured sample course. Files are proxied rather than presigned.
28. **How is processing asynchronous?** FastAPI lifespan owns a bounded asyncio coordinator; blocking providers run in threads. PostgreSQL rows are durable, but there is no Celery/Redis queue.
29. **How did you test it?** 100 focused unittest cases cover boundaries/failures with fakes, plus a 15-question live evaluation. I would not call the suite full end-to-end integration.
30. **What would you improve first?** Add real tenant auth only if product scope requires it; operationally, externalize the worker queue, add Neo4j-down vector fallback/observability, and improve retrieval with BM25 plus an ablation evaluation.

## F. Ten difficult follow-up questions

1. **Could an old worker delete a new attempt's artifacts?** Cleanup is scoped by upload rather than execution token, so overlapping stale/new cleanup remains a theoretical race despite leases/task fencing. Production would write versioned artifacts then atomically publish an active version or condition cleanup by execution token.
2. **What if S3 succeeds and PostgreSQL commit outcome is ambiguous?** The code intentionally retains the content-addressed object to avoid a committed row losing source data. An orphan-reconciliation job is mentioned in comments but not fully scheduled; production needs an outbox/reconciler.
3. **What if object deletion succeeds but PostgreSQL commit fails?** A row can point to a missing object because stores are not transactional. Use a DELETING tombstone/saga, idempotent steps and retries before final row removal.
4. **Why does a no-match graph query return the full graph?** It improves visualization availability but can inject unrelated prerequisite terms. A better design would separate visualization fallback from retrieval expansion and use graph confidence.
5. **Can UUIDv5 point IDs collide?** UUIDv5 collisions are practically negligible, and input includes upload/page/index. The more realistic problem is page-local chunk ordering changing after parser changes, which produces different IDs on a fresh upload.
6. **Is the evidence score calibrated?** No; 70/30 and thresholds are manually configured. Tune on a held-out labelled dataset and monitor refusal/recall trade-offs.
7. **Does source citation prove entailment?** No. It proves retrieval provenance, not that the generated sentence follows from it. Add claim-level citation validation or NLI/LLM verification.
8. **Will multiple Render replicas be safe?** PostgreSQL claims/leases help, but queues, cooldowns, rate limits and retention loops are per process; cross-store cleanup races still need stronger coordination.
9. **Why use Neo4j rather than PostgreSQL edges?** Neo4j makes directional variable-length prerequisite traversal and visualization natural and demonstrates polyglot design, but a bounded two-hop model could still be implemented with recursive SQL at lower operational complexity.
10. **How would you prove GraphRAG helps?** Run the same labelled queries with graph expansion on/off, record retrieval hit@5/MRR, refusal, latency and failure categories, and report confidence intervals or at least paired per-query results.

## G. Five questions where bluffing is easy

1. **“Show the LLM-generated Cypher live path.”** It is not live; `execute_graph_retrieval` calls `_fallback_cypher` directly.
2. **“Where is user ownership enforced?”** Nowhere; the demo has one shared authorization boundary.
3. **“Where is OCR implemented?”** Nowhere; scanned PDFs are rejected after native-text extraction yields no chunks.
4. **“Show BM25/hybrid fusion.”** It does not exist; search is one dense query with graph-added text.
5. **“What improvement did GraphRAG achieve over vector RAG?”** No ablation exists; the baseline measures the combined system only.

## H. Five strongest engineering decisions

1. Persist source and processing state before queue submission, allowing queue-full deferral and restart recovery.
2. Use leases, heartbeats, row locks and task IDs to fence stale processing attempts.
3. Separate vector readiness from graph quality so partial provider output does not destroy searchable data.
4. Validate LLM graph schema, endpoints, relationship allowlist and source provenance before storage.
5. Implement idempotent upload-scoped cleanup/deletion and provider failover with grounded degradation.

## I. Five weakest architecture areas

1. Shared demo secret means no identity, tenant isolation, per-user quota or private PDFs.
2. In-process queue, limiter, scheduler and circuit breaker do not coordinate horizontally.
3. Cross-store writes/deletion lack a durable saga/outbox/tombstone and can be partially complete.
4. Retrieval is substring anchoring + bounded one-hop/two-hop prerequisite expansion + dense search; no BM25 or measured graph uplift.
5. Native-text-only parsing, whole-file memory handling and no prompt-injection/malware controls limit document robustness.

## J. Five rebuild improvements

1. Start with PostgreSQL migrations plus pgvector/edge tables, then add Qdrant/Neo4j only if benchmarks justify them.
2. Use a durable queue and versioned ingestion artifacts with an outbox/saga and dead-letter handling.
3. Add tenant-aware OAuth/session identity, row ownership, storage prefixes and store-level filters if moving beyond a shared demo.
4. Add OCR/layout/table extraction and a sandboxed PDF ingestion boundary.
5. Build hybrid BM25+dense retrieval, confidence-aware graph expansion, answer-citation verification and vector-only degradation; evaluate each addition through ablations.

## K. Architecture diagram

```text
                                  RENDER
 Browser
   |
   | HTTPS + HttpOnly signed demo cookie
   v
+----------------------+       +--------------------------------------+
| React/Vite static UI |------>| FastAPI modular monolith             |
| Dashboard/Cytoscape  |       | middleware + routers                 |
+----------------------+       +------------------+-------------------+
                                                  |
                  +-------------------------------+------------------+
                  |                               |                  |
                  v                               v                  v
          +---------------+               +---------------+  +---------------+
          | PostgreSQL    |               | Object storage|  | AI providers  |
          | source truth  |               | PDF bytes     |  | Groq->Cerebras|
          | course/upload |               | S3/R2/MinIO   |  | Cohere        |
          | attempts/lease|               +---------------+  +---------------+
          +-------+-------+
                  |
                  | durable UPLOADED rows
                  v
          +--------------------+
          | ProcessingCoordinator (in API process)
          | bounded asyncio queue + dispatcher + worker
          +--------------------+
                  |
                  | PyMuPDF -> 500-word/50-overlap page chunks
          +-------+----------------------------------+
          |                                          |
          v                                          v
  +------------------+                       +------------------+
  | Qdrant           |                       | Neo4j            |
  | 384-d cosine     |                       | Course/Concept   |
  | chunk + payload  |                       | six edge types   |
  +--------+---------+                       +--------+---------+
           |                                          |
           | filtered dense top 10                    | term match + 1 hop
           +--------------------+---------------------+
                                v
                     prerequisite query expansion
                                |
                                v
                         Cohere reranking
                                |
                         threshold + top 5
                                |
                                v
                     Groq/Cerebras synthesis
                                |
                                v
                    answer + confidence + sources
```

## L. Technology decision table

| Technology | Why used | Where used | Alternative | Trade-off |
| --- | --- | --- | --- | --- |
| React/TypeScript | Typed interactive dashboard | `src/` | Vue/Svelte/server templates | Good ecosystem; client state/polling complexity |
| Vite | Fast build/code splitting | `vite.config.ts`, `App.tsx` | Next.js/Webpack | Simple SPA; no SSR or framework routing |
| Cytoscape.js | Graph layout and interaction | `ConceptGraphCanvas.tsx` | D3/React Flow | Graph-native; large lazy chunk/layout cost |
| FastAPI/Pydantic | Async APIs and validation | `app/main.py`, endpoints/schemas | Django/Flask | Clear typed contracts; no built-in durable jobs/admin |
| SQLAlchemy/asyncpg | Async PostgreSQL access | `core/database.py`, models/services | raw SQL/psycopg | Testable model layer; mixed ORM/raw migration complexity |
| PostgreSQL | Durable source of truth/leases | `Course`, `DocumentUpload`, `ProcessingAttempt` | SQLite/DynamoDB | Strong transactions/locks; external service and schema ops |
| PyMuPDF | Fast native PDF text/pages | `parser_service.py`, admission | pdfplumber/Tika | Page provenance; no OCR/layout semantics |
| Qdrant | Dense vector inference/search/filtering | `ingestion_service.py`, `rag_service.py` | pgvector/Pinecone/Weaviate | Specialized search; another consistency boundary |
| MiniLM 384-d | Low-cost compact embeddings | config/Render/Qdrant | larger BGE/OpenAI embeddings | Cheap/fast; lower domain accuracy |
| Cohere Rerank | Improve top-10 precision | `rerank_service.py` | local cross-encoder/Jina | Low API memory; provider cost/quota/latency |
| Neo4j | Directional concepts/provenance | ingestion/retrieval services | PostgreSQL recursive query/NetworkX | Natural bounded traversal; operational weight for project scale |
| Groq | Fast primary generation | graph/QA/exam services | OpenAI/Anthropic/Gemini | Fast/free-tier friendly; quotas/schema variability |
| Cerebras | Secondary model failover | `cerebras_service.py` + generators | another Groq model/AI gateway | Resilience; duplicated provider logic and second secret |
| S3/R2/MinIO | Store binary PDFs | `storage_service.py` | DB blobs/local disk | Cheap durable objects; nontransactional and proxy overhead |
| asyncio coordinator | Minimal background processing | `processing_coordinator.py` | Celery/RQ/SQS | Easy single-service deploy; process-local scale limits |
| Render Blueprint/Docker | Reproducible portfolio deploy | `render.yaml`, Dockerfiles | Fly.io/Kubernetes | Simple/free; sleeps/limits and external manual resources |

## M. Failure-mode table

| Component | Failure | Current handling | Better production approach |
| --- | --- | --- | --- |
| PostgreSQL | Down/commit ambiguous | Health 503; requests fail; source object retained on ambiguous admission commit | HA DB, outbox/reconciliation, metrics/alerts |
| In-process worker | Crash/restart | Lease expiry, stale recovery, new attempt, cleanup | Durable external queue, visibility timeout, DLQ |
| PyMuPDF | malformed/encrypted/no text | 400 admission or permanent DOCUMENT_ERROR | Sandboxed parser, OCR/layout fallback, malware scan |
| Qdrant | down/DNS/dimension mismatch | Retryable FAILED/503; dimension validated at startup/use | Replica/backup, retry budget, vector-only reconciliation |
| Neo4j | down | Retryable ingestion failure or query 503 | Vector-only query degradation, replicas/circuit breaker |
| Object storage | down/missing | 503 or permanent missing-source failure; READY verifies existence | Presigned streaming, versioning, reconciliation |
| Groq | quota/timeout/5xx | Cerebras failover and process-local cooldown | Central provider router, distributed breaker/budgets |
| Cerebras | quota/timeout | Partial graph, evidence-only Q&A or exam 503 | Third fallback or queued generation with SLOs |
| Cohere | failure/invalid indexes | Query returns reranking 503 | Local/fallback ranker, retry/circuit breaker |
| LLM malformed graph | Schema fallback/failover | Drop batch, preserve successes, partial/empty status | Repair model, offline retry queue, quality evaluation |
| LLM hallucinated answer | Prompt/threshold/citations only | No claim-level verification | Entailment/citation checker and guarded output |
| Cross-store deletion | partial step | 503 + repeatable operations | DELETING state, saga/outbox, background reconciliation |
| Rate limiter | restart/multiple replicas | Counters reset/diverge | Redis/API-gateway distributed limits |
| Frontend/API | timeout/network | Abort and readable local error | Idempotent retry policy, request IDs, observability |

## N. Database-responsibility table

| Store | Authoritative responsibility | Key data | Must not be described as |
| --- | --- | --- | --- |
| PostgreSQL | Source of truth for course identity, document lifecycle and processing attempts | course UUID/name; upload/task/hash/storage key; stages, leases, counts, errors, result JSON | Vector search engine or PDF blob store |
| Qdrant | Searchable chunk vectors and retrieval payload | 384-d embedding; chunk text; upload/course/execution IDs; filename/page/section | Source of lifecycle truth or graph database |
| Neo4j | Explainable concept topology and provenance | Course/Concept nodes, CONTAINS, six directional relationship types, source properties | Full document corpus, multi-hop reasoning engine, or authoritative factual source |
| Object storage | Durable source PDF bytes | Content-addressed private PDF object | Metadata/control database or user authorization system |
