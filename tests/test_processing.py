import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, Mock, patch

import pymupdf as fitz
import httpx
from botocore.exceptions import ClientError, EndpointConnectionError
from fastapi import HTTPException, Request, UploadFile
from groq import BadRequestError
from starlette.responses import JSONResponse
from starlette.datastructures import Headers

from app.api.endpoints import auth, ingest
from app.core.config import Settings
from app.core.processing import FailureCategory, ProcessingStage, classify_failure, normalize_course_name
from app.core.processing_coordinator import EnqueueDisposition, ProcessingCoordinator
from app.core.security import DemoProtectionMiddleware
from app.schemas.auth import AccessCodeRequest
from app.services.citation_service import assess_evidence, build_sources
from app.services.course_service import CourseNotReadyError, CourseService
from app.services.document_processing_service import DocumentProcessingService
from app.services.exam_service import ExamService
from app.services.ingestion_service import IngestionService
from app.services.parser_service import DocumentChunk
from app.services.rag_service import RetrievalService
from app.services.rerank_service import RerankService
from app.services.security_service import DemoAccessService, RateLimitResult, RateLimitService
from app.services.storage_service import ObjectStorageError, StorageService
from app.services.upload_service import UploadService
from app.schemas.exam import ExamSource
from app.schemas.extraction import ConceptNode, ConceptRelationship, GraphExtractionResponse
from pydantic import SecretStr, ValidationError


class ProcessingRulesTests(unittest.TestCase):
    def test_course_normalization_collapses_case_and_space(self):
        self.assertEqual(normalize_course_name("  CYBER  "), "cyber")
        self.assertEqual(normalize_course_name("Cyber"), "cyber")
        self.assertEqual(normalize_course_name("cyber"), "cyber")

    def test_qdrant_api_key_is_unwrapped_only_for_the_client(self):
        config = Settings(
            _env_file=None,
            QDRANT_API_KEY="qdrant-secret",
        )

        self.assertEqual(config.qdrant_api_key_value, "qdrant-secret")
        self.assertNotIn("qdrant-secret", repr(config.qdrant_api_key))

    def test_demo_cookie_is_signed_tamper_resistant_and_expires(self):
        config = Settings(
            _env_file=None,
            DEMO_ACCESS_TOKEN="a-strong-demo-secret-value-123456",
            AUTH_SESSION_TTL_SECONDS=300,
        )
        service = DemoAccessService(config)
        cookie = service.issue_cookie(now=1_000)

        self.assertTrue(service.verify_cookie(cookie, now=1_299))
        self.assertFalse(service.verify_cookie(f"{cookie}tampered", now=1_299))
        self.assertFalse(service.verify_cookie(cookie, now=1_301))
        self.assertFalse(service.verify_cookie(cookie, now=900))

    def test_demo_access_token_must_be_high_entropy(self):
        with self.assertRaises(ValidationError):
            Settings(_env_file=None, DEMO_ACCESS_TOKEN="short-token")

    def test_strict_public_startup_rejects_missing_secrets(self):
        with self.assertRaises(ValidationError) as raised:
            Settings(_env_file=None, STRICT_STARTUP_VALIDATION=True)

        self.assertIn("Missing required public-deployment", str(raised.exception))

    def test_strict_hosted_configuration_accepts_complete_secrets(self):
        config = Settings(
            _env_file=None,
            STRICT_STARTUP_VALIDATION=True,
            DATABASE_URL="postgresql://user:pass@db.example.test/conceptgraph",
            NEO4J_URI="neo4j+s://graph.example.test",
            NEO4J_USERNAME="neo4j",
            NEO4J_PASSWORD="graph-secret",
            QDRANT_URL="https://vectors.example.test",
            QDRANT_API_KEY="vector-secret",
            EMBEDDING_PROVIDER="qdrant_cloud",
            RERANK_PROVIDER="cohere",
            COHERE_API_KEY="cohere-secret",
            OBJECT_STORAGE_BACKEND="s3",
            S3_BUCKET="private-pdfs",
            S3_ENDPOINT_URL="https://account.r2.cloudflarestorage.com",
            S3_ACCESS_KEY_ID="r2-access",
            S3_SECRET_ACCESS_KEY="r2-secret",
            ALLOWED_ORIGINS="https://portfolio.example.test",
            DEMO_ACCESS_TOKEN="a-strong-demo-secret-value-123456",
            REQUIRE_UPLOAD_AUTH=True,
            LLM_PROVIDER="groq",
            GROQ_API_KEY="groq-secret",
        )

        self.assertTrue(config.strict_startup_validation)
        self.assertEqual(config.embedding_provider, "qdrant_cloud")

    def test_rate_limit_is_enforced_by_the_single_process_counter(self):
        service = RateLimitService()

        first = asyncio.run(service.check("query:user", 2, now=120))
        second = asyncio.run(service.check("query:user", 2, now=121))
        third = asyncio.run(service.check("query:user", 2, now=122))

        self.assertTrue(first.allowed)
        self.assertEqual(second.remaining, 0)
        self.assertFalse(third.allowed)
        self.assertEqual(third.retry_after, 58)

    def test_protected_route_rejects_a_missing_credential(self):
        config = Settings(
            _env_file=None,
            DEMO_ACCESS_TOKEN="a-strong-demo-secret-value-123456",
        )
        access_service = DemoAccessService(config)
        middleware = DemoProtectionMiddleware(AsyncMock())
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/api/v1/ingest/courses",
                "query_string": b"",
                "headers": [],
                "scheme": "https",
                "server": ("api.example.com", 443),
                "client": ("127.0.0.1", 1234),
            }
        )
        call_next = AsyncMock(return_value=JSONResponse({"ok": True}))

        with patch("app.core.security.demo_access_service", access_service):
            response = asyncio.run(middleware.dispatch(request, call_next))

        self.assertEqual(response.status_code, 401)
        call_next.assert_not_awaited()

    def test_valid_bearer_is_rate_limited_and_forwarded(self):
        token = "a-strong-demo-secret-value-123456"
        config = Settings(_env_file=None, DEMO_ACCESS_TOKEN=token)
        access_service = DemoAccessService(config)
        limiter = SimpleNamespace(
            check=AsyncMock(
                return_value=RateLimitResult(
                    allowed=True,
                    limit=300,
                    remaining=299,
                    retry_after=30,
                )
            )
        )
        middleware = DemoProtectionMiddleware(AsyncMock())
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/api/v1/ingest/courses",
                "query_string": b"",
                "headers": [(b"authorization", f"Bearer {token}".encode())],
                "scheme": "https",
                "server": ("api.example.com", 443),
                "client": ("127.0.0.1", 1234),
            }
        )
        call_next = AsyncMock(return_value=JSONResponse({"ok": True}))

        with (
            patch("app.core.security.demo_access_service", access_service),
            patch("app.core.security.rate_limit_service", limiter),
        ):
            response = asyncio.run(middleware.dispatch(request, call_next))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["X-RateLimit-Remaining"], "299")
        call_next.assert_awaited_once()

    def test_access_code_exchange_sets_an_httponly_cookie(self):
        token = "a-strong-demo-secret-value-123456"
        access_service = DemoAccessService(
            Settings(_env_file=None, DEMO_ACCESS_TOKEN=token)
        )
        limiter = SimpleNamespace(
            check=AsyncMock(
                return_value=RateLimitResult(
                    allowed=True,
                    limit=10,
                    remaining=9,
                    retry_after=30,
                )
            )
        )
        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/v1/auth/session",
                "query_string": b"",
                "headers": [],
                "scheme": "https",
                "server": ("api.example.com", 443),
                "client": ("127.0.0.1", 1234),
            }
        )
        response = JSONResponse({})

        with (
            patch("app.api.endpoints.auth.demo_access_service", access_service),
            patch("app.api.endpoints.auth.rate_limit_service", limiter),
        ):
            session = asyncio.run(
                auth.create_session(
                    AccessCodeRequest(access_code=token),
                    request,
                    response,
                )
            )

        self.assertTrue(session.authenticated)
        set_cookie = response.headers["set-cookie"]
        self.assertIn("HttpOnly", set_cookie)
        self.assertIn("SameSite=lax", set_cookie)
        self.assertNotIn(token, set_cookie)

    def test_configuration_failure_is_permanent(self):
        category, retryable, _ = classify_failure(RuntimeError("GROQ_API_KEY is not configured"))
        self.assertEqual(category, FailureCategory.CONFIGURATION_ERROR)
        self.assertFalse(retryable)

    def test_unavailable_model_is_a_permanent_configuration_failure(self):
        category, retryable, message = classify_failure(
            RuntimeError(
                "The model `retired-model` does not exist or you do not have access to it."
            )
        )

        self.assertEqual(category, FailureCategory.CONFIGURATION_ERROR)
        self.assertFalse(retryable)
        self.assertIn("model is unavailable", message)

    def test_existing_qdrant_collection_gets_upload_filter_index(self):
        vector_client = SimpleNamespace(
            get_collections=lambda: SimpleNamespace(
                collections=[SimpleNamespace(name="conceptgraph_chunks")]
            ),
            get_collection=lambda name: SimpleNamespace(
                payload_schema={},
                config=SimpleNamespace(
                    params=SimpleNamespace(vectors=SimpleNamespace(size=384))
                ),
            ),
            collection_exists=lambda name: True,
            create_payload_index=Mock(),
        )
        service = IngestionService(
            graph_driver=SimpleNamespace(),
            vector_client=vector_client,
        )

        service._ensure_qdrant_collection(vector_size=384)

        vector_client.create_payload_index.assert_called_once_with(
            collection_name="conceptgraph_chunks",
            field_name="upload_id",
            field_schema=ANY,
            wait=True,
        )

    def test_incompatible_qdrant_dimension_is_rejected(self):
        vector_client = SimpleNamespace(
            collection_exists=lambda name: True,
            get_collection=lambda name: SimpleNamespace(
                config=SimpleNamespace(
                    params=SimpleNamespace(vectors=SimpleNamespace(size=768))
                )
            ),
        )
        service = IngestionService(
            graph_driver=SimpleNamespace(),
            vector_client=vector_client,
        )

        with self.assertRaises(RuntimeError) as raised:
            service.validate_qdrant_collection()

        self.assertIn("new collection name", str(raised.exception))

    def test_qdrant_cloud_inference_sends_documents_instead_of_local_vectors(self):
        vector_client = SimpleNamespace(
            get_collections=lambda: SimpleNamespace(collections=[]),
            create_collection=Mock(),
            get_collection=lambda name: SimpleNamespace(payload_schema={}),
            create_payload_index=Mock(),
            upsert=Mock(),
        )
        chunk = DocumentChunk(
            id="chunk-1",
            text="Hosted embedding text",
            metadata={"upload_id": "upload-1"},
        )
        service = IngestionService(
            graph_driver=SimpleNamespace(),
            vector_client=vector_client,
        )

        with (
            patch("app.services.ingestion_service.settings.embedding_provider", "qdrant_cloud"),
            patch(
                "app.services.ingestion_service.settings.embedding_model_name",
                "sentence-transformers/all-MiniLM-L6-v2",
            ),
        ):
            indexed = service.upsert_chunks_to_qdrant([chunk])

        self.assertEqual(indexed, 1)
        point = vector_client.upsert.call_args.kwargs["points"][0]
        self.assertEqual(point.vector.text, chunk.text)
        self.assertEqual(
            point.vector.model,
            "sentence-transformers/all-MiniLM-L6-v2",
        )

    def test_cohere_reranking_preserves_probability_scoring_contract(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/v2/rerank")
            self.assertEqual(request.headers["authorization"], "Bearer cohere-secret")
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"index": 1, "relevance_score": 0.9},
                        {"index": 0, "relevance_score": 0.2},
                    ]
                },
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        chunks = [
            {"id": "a", "text": "weak"},
            {"id": "b", "text": "strong"},
        ]
        with (
            patch("app.services.rerank_service.settings.rerank_provider", "cohere"),
            patch("app.services.rerank_service.settings.rerank_model_name", "rerank-v3.5"),
            patch(
                "app.services.rerank_service.settings.cohere_api_key",
                SecretStr("cohere-secret"),
            ),
        ):
            ranked = asyncio.run(RerankService(client).rerank("query", chunks))
        asyncio.run(client.aclose())

        self.assertEqual([chunk["id"] for chunk in ranked], ["b", "a"])
        probability = 1 / (1 + __import__("math").exp(-ranked[0]["rerank_score"]))
        self.assertAlmostEqual(probability, 0.9)

    def test_groq_graph_extraction_uses_strict_json_schema(self):
        completion = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content='{"nodes": [], "relationships": []}')
                )
            ]
        )
        client = Mock()
        client.chat.completions.create.return_value = completion
        service = IngestionService(
            graph_driver=SimpleNamespace(),
            vector_client=SimpleNamespace(),
        )

        with (
            patch("app.services.ingestion_service.settings.groq_api_key", "test-key"),
            patch("app.services.ingestion_service.Groq", return_value=client),
        ):
            result = service._extract_with_groq("Course text")

        response_format = client.chat.completions.create.call_args.kwargs[
            "response_format"
        ]
        self.assertEqual(response_format["type"], "json_schema")
        self.assertTrue(response_format["json_schema"]["strict"])
        self.assertEqual(
            set(response_format["json_schema"]["schema"]["required"]),
            {"nodes", "relationships"},
        )
        self.assertEqual(result, GraphExtractionResponse())

    def test_json_schema_provider_failure_is_retryable(self):
        category, retryable, message = classify_failure(
            RuntimeError("Groq error code: json_validate_failed")
        )

        self.assertEqual(category, FailureCategory.PROVIDER_ERROR)
        self.assertTrue(retryable)
        self.assertIn("structure", message)

    def test_groq_retries_json_validation_with_smaller_context(self):
        failure = BadRequestError(
            "Failed to validate JSON",
            response=httpx.Response(
                400,
                request=httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions"),
            ),
            body={"error": {"code": "json_validate_failed"}},
        )
        completion = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content='{"nodes": [], "relationships": []}')
                )
            ]
        )
        client = Mock()
        client.chat.completions.create.side_effect = [failure, completion]
        service = IngestionService(
            graph_driver=SimpleNamespace(),
            vector_client=SimpleNamespace(),
        )

        with (
            patch("app.services.ingestion_service.settings.groq_api_key", "test-key"),
            patch("app.services.ingestion_service.Groq", return_value=client),
        ):
            result = service._extract_with_groq("Course text\n" * 500)

        self.assertEqual(client.chat.completions.create.call_count, 2)
        first_context = client.chat.completions.create.call_args_list[0].kwargs["messages"][1]["content"]
        second_context = client.chat.completions.create.call_args_list[1].kwargs["messages"][1]["content"]
        self.assertLess(len(second_context), len(first_context))
        self.assertEqual(result, GraphExtractionResponse())

    def test_groq_does_not_retry_unrelated_bad_requests(self):
        failure = BadRequestError(
            "Invalid request",
            response=httpx.Response(
                400,
                request=httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions"),
            ),
            body={"error": {"code": "invalid_request_error"}},
        )
        client = Mock()
        client.chat.completions.create.side_effect = failure
        service = IngestionService(
            graph_driver=SimpleNamespace(),
            vector_client=SimpleNamespace(),
        )

        with (
            patch("app.services.ingestion_service.settings.groq_api_key", "test-key"),
            patch("app.services.ingestion_service.Groq", return_value=client),
            self.assertRaises(BadRequestError),
        ):
            service._extract_with_groq("Course text")

        self.assertEqual(client.chat.completions.create.call_count, 1)

    def test_worker_failure_is_retryable(self):
        category, retryable, _ = classify_failure(RuntimeError("worker interrupted"))
        self.assertEqual(category, FailureCategory.WORKER_ERROR)
        self.assertTrue(retryable)

    def test_qdrant_dns_failure_is_retryable_storage_error(self):
        category, retryable, message = classify_failure(
            RuntimeError(
                "ResponseHandlingException: nodename nor servname provided, or not known"
            )
        )

        self.assertEqual(category, FailureCategory.DATABASE_ERROR)
        self.assertTrue(retryable)
        self.assertIn("storage service", message)

    def test_citations_deduplicate_and_hide_internal_ids(self):
        chunk = {
            "id": "internal-vector-uuid",
            "text": "Supporting passage",
            "score": 0.8,
            "metadata": {
                "upload_id": "document-uuid",
                "document_name": "Cyber.pdf",
                "page_number": 6,
                "section_heading": "Cyber Hygiene",
            },
        }
        sources = build_sources([chunk, chunk])
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["source_id"], "source-1")
        self.assertNotIn("internal-vector-uuid", str(sources))
        self.assertEqual(sources[0]["document_name"], "Cyber.pdf")

    def test_graph_rejects_relationships_with_missing_entities(self):
        with self.assertRaises(ValidationError):
            GraphExtractionResponse(
                nodes=[ConceptNode(id="known", name="Known", type="concept")],
                relationships=[
                    ConceptRelationship(
                        source_node_id="missing",
                        target_node_id="known",
                        relation_type="PREREQUISITE_OF",
                    )
                ],
            )

    def test_graph_deduplicates_identical_relationships(self):
        relationship = ConceptRelationship(
            source_node_id="a",
            target_node_id="b",
            relation_type="RELATED_TO",
        )
        graph = GraphExtractionResponse(
            nodes=[
                ConceptNode(id="a", name="A", type="concept"),
                ConceptNode(id="b", name="B", type="concept"),
            ],
            relationships=[relationship, relationship],
        )
        self.assertEqual(len(graph.relationships), 1)

    def test_neo4j_relationship_uses_mapping_interface(self):
        class FakeRelationship:
            type = "PREREQUISITE_OF"
            start_node = {"id": "source"}
            end_node = {"id": "target"}

            def items(self):
                return [("document_name", "Course.pdf")]

            def __iter__(self):
                return iter(["not-a-key-value-pair"])

        serialized = RetrievalService._relationship_to_dict(FakeRelationship())

        self.assertEqual(serialized["type"], "PREREQUISITE_OF")
        self.assertEqual(serialized["source"], "source")
        self.assertEqual(serialized["target"], "target")
        self.assertEqual(serialized["document_name"], "Course.pdf")

    def test_bidirectional_graph_query_preserves_native_edge_direction(self):
        generated = RetrievalService._fallback_cypher("zero trust")

        self.assertIn("(concept)-[relationship]-(related:Concept)", generated.cypher)
        self.assertIn("related_concepts", generated.cypher)

    def test_evidence_threshold_removes_irrelevant_passages(self):
        relevant = {
            "text": "Strongly relevant passage",
            "score": 0.82,
            "rerank_score": 5.0,
            "metadata": {},
        }
        irrelevant = {
            "text": "Unrelated passage",
            "score": 0.05,
            "rerank_score": -10.0,
            "metadata": {},
        }

        chunks, confidence = assess_evidence([irrelevant, relevant])

        self.assertEqual([chunk["text"] for chunk in chunks], [relevant["text"]])
        self.assertEqual(confidence["evidence_count"], 1)
        self.assertIn(confidence["level"], {"medium", "low"})

    def test_exam_sources_are_balanced_and_citations_are_enriched(self):
        chunks = [
            {
                "text": "Alpha evidence",
                "metadata": {
                    "document_name": "A.pdf",
                    "page_number": 1,
                    "section_heading": "Alpha",
                    "chunk_index": 0,
                },
            },
            {
                "text": "Beta evidence",
                "metadata": {
                    "document_name": "B.pdf",
                    "page_number": 4,
                    "section_heading": "Beta",
                    "chunk_index": 0,
                },
            },
        ]
        sources = ExamService._select_exam_sources(chunks)
        raw = """{
          "questions": [{
            "question_text": "Which statement is supported?",
            "options": ["Alpha", "One", "Two", "Three"],
            "correct_answer": "Alpha",
            "explanation": "Alpha is supported by the cited passage.",
            "topic": "Alpha",
            "citation_ids": ["exam-source-1"]
          }]
        }"""

        questions = ExamService._parse_questions(raw, sources)

        self.assertEqual({source.document_name for source in sources}, {"A.pdf", "B.pdf"})
        self.assertEqual(questions[0].sources[0].source_id, "exam-source-1")
        self.assertTrue(questions[0].sources[0].supporting_passage)

    def test_exam_question_without_valid_citation_is_rejected(self):
        source = ExamSource(
            source_id="exam-source-1",
            document_name="Course.pdf",
            page_number=1,
            supporting_passage="Evidence",
        )
        raw = """{
          "questions": [{
            "question_text": "Unsupported?",
            "options": ["A", "B", "C", "D"],
            "correct_answer": "A",
            "explanation": "No valid citation.",
            "topic": "Test",
            "citation_ids": ["invented-source"]
          }]
        }"""

        self.assertEqual(ExamService._parse_questions(raw, [source]), [])

    def test_render_postgres_url_uses_asyncpg_driver(self):
        config = Settings(DATABASE_URL="postgresql://user:password@db/course")

        self.assertEqual(
            config.postgres_dsn,
            "postgresql+asyncpg://user:password@db/course",
        )

    def test_neon_postgres_url_normalizes_libpq_ssl_parameters(self):
        config = Settings(
            DATABASE_URL=(
                "postgresql://user:password@db.neon.tech/course"
                "?sslmode=require&channel_binding=require"
            )
        )

        self.assertEqual(
            config.postgres_dsn,
            "postgresql+asyncpg://user:password@db.neon.tech/course?ssl=require",
        )

    def test_pdf_preview_supports_byte_ranges(self):
        response = ingest._pdf_response(b"0123456789", "Course Notes.pdf", "bytes=2-5")

        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.body, b"2345")
        self.assertEqual(response.headers["content-range"], "bytes 2-5/10")


class StorageServiceTests(unittest.TestCase):
    class FakeBody:
        def __init__(self, content: bytes):
            self.content = content
            self.closed = False

        def read(self):
            return self.content

        def close(self):
            self.closed = True

    class FakeS3:
        def __init__(self):
            self.objects: dict[tuple[str, str], bytes] = {}

        def head_bucket(self, **kwargs):
            return {}

        def put_object(self, **kwargs):
            self.objects[(kwargs["Bucket"], kwargs["Key"])] = kwargs["Body"]

        def get_object(self, **kwargs):
            return {
                "Body": StorageServiceTests.FakeBody(
                    self.objects[(kwargs["Bucket"], kwargs["Key"])]
                )
            }

        def head_object(self, **kwargs):
            self.objects[(kwargs["Bucket"], kwargs["Key"])]
            return {}

        def delete_object(self, **kwargs):
            self.objects.pop((kwargs["Bucket"], kwargs["Key"]), None)

    def test_s3_round_trip_uses_content_addressed_key(self):
        client = self.FakeS3()
        config = Settings(
            OBJECT_STORAGE_BACKEND="s3",
            S3_BUCKET="test-pdfs",
            S3_ENDPOINT_URL="https://objects.example.test",
            S3_AUTO_CREATE_BUCKET=False,
        )
        service = StorageService(config, client=client)
        key = service.object_key("course-1", "abc123")

        service.put_pdf(key, b"pdf-content")

        self.assertEqual(key, "courses/course-1/documents/abc123.pdf")
        self.assertTrue(service.exists(key))
        self.assertEqual(service.get_bytes(key), b"pdf-content")
        service.delete(key)
        self.assertFalse(client.objects)

    def test_readiness_creates_a_missing_bucket_when_local_auto_create_is_enabled(self):
        class MissingBucket(self.FakeS3):
            def __init__(self):
                super().__init__()
                self.bucket_exists = False

            def head_bucket(self, **kwargs):
                if not self.bucket_exists:
                    raise ClientError(
                        {
                            "Error": {"Code": "NoSuchBucket"},
                            "ResponseMetadata": {"HTTPStatusCode": 404},
                        },
                        "HeadBucket",
                    )
                return {}

            def create_bucket(self, **kwargs):
                self.bucket_exists = True

        client = MissingBucket()
        config = Settings(
            OBJECT_STORAGE_BACKEND="s3",
            S3_BUCKET="test-pdfs",
            S3_ENDPOINT_URL="http://localhost:9000",
            S3_AUTO_CREATE_BUCKET=True,
        )

        StorageService(config, client=client).check_ready()

        self.assertTrue(client.bucket_exists)

    def test_legacy_local_reference_remains_readable_for_migration(self):
        with tempfile.TemporaryDirectory() as upload_dir:
            path = Path(upload_dir) / "legacy.pdf"
            path.write_bytes(b"legacy-pdf")
            config = Settings(
                OBJECT_STORAGE_BACKEND="local",
                LEGACY_UPLOAD_DIR=Path(upload_dir),
            )
            service = StorageService(config)

            self.assertTrue(service.is_legacy_reference(str(path)))
            self.assertEqual(service.get_bytes(str(path)), b"legacy-pdf")

    def test_network_failure_is_normalized_to_storage_error(self):
        client = self.FakeS3()
        client.head_bucket = lambda **kwargs: (_ for _ in ()).throw(
            EndpointConnectionError(endpoint_url="https://objects.example.test")
        )
        config = Settings(
            OBJECT_STORAGE_BACKEND="s3",
            S3_BUCKET="test-pdfs",
            S3_ENDPOINT_URL="https://objects.example.test",
        )

        with self.assertRaises(ObjectStorageError):
            StorageService(config, client=client).put_pdf("document.pdf", b"pdf")


class UploadEndpointTests(unittest.TestCase):
    def test_upload_writes_content_addressed_object_before_enqueuing(self):
        document = fitz.open()
        page = document.new_page()
        page.insert_text((72, 72), "Object storage test")
        content = document.tobytes()
        document.close()
        upload = UploadFile(
            BytesIO(content),
            size=len(content),
            filename="course.pdf",
            headers=Headers({"content-type": "application/pdf"}),
        )
        course = SimpleNamespace(id="course-uuid", display_name="CYBER")
        db = SimpleNamespace(execute=AsyncMock(), rollback=AsyncMock())

        with (
            patch.object(
                ingest.course_service,
                "get_or_create",
                AsyncMock(return_value=course),
            ),
            patch.object(
                ingest.upload_service,
                "find_duplicate",
                AsyncMock(return_value=None),
            ),
            patch.object(
                ingest.upload_service,
                "count_uploads",
                AsyncMock(return_value=0),
            ),
            patch.object(ingest.upload_service, "create_upload", AsyncMock()),
            patch.object(ingest.storage_service, "put_pdf") as put_pdf,
            patch.object(
                ingest.processing_coordinator,
                "submit",
                AsyncMock(return_value=EnqueueDisposition.ACCEPTED),
            ) as enqueue,
        ):
            response = asyncio.run(ingest.upload_document(" CYBER ", upload, db))

        key = put_pdf.call_args.args[0]
        self.assertTrue(key.startswith("courses/course-uuid/documents/"))
        self.assertTrue(key.endswith(".pdf"))
        self.assertEqual(put_pdf.call_args.args[1], content)
        self.assertEqual(response.task_id, enqueue.await_args.args[1])
        self.assertEqual(enqueue.await_args.args[0], response.upload_id)

    def test_failed_duplicate_deletion_keeps_shared_object(self):
        record = SimpleNamespace(
            upload_id="failed-duplicate",
            storage_key="courses/course-1/documents/hash.pdf",
        )
        db = SimpleNamespace(rollback=AsyncMock())

        with (
            patch.object(
                ingest.upload_service,
                "lock_failed_for_deletion",
                AsyncMock(return_value=record),
            ),
            patch.object(
                ingest.upload_service,
                "storage_key_is_shared",
                AsyncMock(return_value=True),
            ),
            patch.object(
                ingest.upload_service,
                "delete_failed",
                AsyncMock(return_value=record),
            ),
            patch.object(ingest.storage_service, "delete") as delete_object,
        ):
            asyncio.run(ingest.remove_failed_upload(record.upload_id, db))

        delete_object.assert_not_called()

    def test_failed_deletion_cleans_partial_derived_artifacts(self):
        record = SimpleNamespace(
            upload_id="failed-upload",
            course_uuid="course-1",
            storage_key="courses/course-1/documents/hash.pdf",
        )
        db = SimpleNamespace(rollback=AsyncMock())

        with (
            patch.object(
                ingest.upload_service,
                "lock_failed_for_deletion",
                AsyncMock(return_value=record),
            ),
            patch.object(
                ingest.document_processing_service.ingestion_service,
                "cleanup_upload",
                AsyncMock(),
            ) as cleanup,
            patch.object(
                ingest.upload_service,
                "storage_key_is_shared",
                AsyncMock(return_value=True),
            ),
            patch.object(
                ingest.upload_service,
                "delete_failed",
                AsyncMock(return_value=record),
            ),
        ):
            asyncio.run(ingest.remove_failed_upload(record.upload_id, db))

        cleanup.assert_awaited_once_with("failed-upload", "course-1")


class ProcessingFencingTests(unittest.TestCase):
    def test_stale_or_failed_attempt_cannot_advance_stage(self):
        service = UploadService()
        service._get_current_upload = AsyncMock(
            return_value=SimpleNamespace(status="failed")
        )
        session = SimpleNamespace(commit=AsyncMock())

        updated = asyncio.run(
            service.set_stage(
                session,
                "upload-1",
                "stale-task",
                ProcessingStage.EMBEDDING,
            )
        )

        self.assertFalse(updated)
        session.commit.assert_not_awaited()

    def test_exhausted_retry_uses_terminal_corrective_action(self):
        service = UploadService()
        record = SimpleNamespace(
            status="active",
            attempt_count=3,
        )
        attempt = SimpleNamespace()
        service._get_current_upload = AsyncMock(return_value=record)
        service._current_attempt = AsyncMock(return_value=attempt)
        session = SimpleNamespace(commit=AsyncMock())

        updated = asyncio.run(
            service.mark_failed(
                session,
                "upload-1",
                "task-3",
                "The provider failed. Please retry.",
                FailureCategory.PROVIDER_ERROR,
                True,
            )
        )

        self.assertTrue(updated)
        self.assertFalse(record.retryable)
        self.assertIn("Remove this failed record", record.error_message)
        self.assertEqual(attempt.error_message, record.error_message)

    def test_ready_transition_requires_graph_built_and_committed_chunks(self):
        service = UploadService()
        record = SimpleNamespace(
            status="active",
            stage=ProcessingStage.EMBEDDED.value,
            course_uuid="course-1",
            storage_key="courses/course-1/documents/hash.pdf",
        )
        service._get_current_upload = AsyncMock(return_value=record)
        session = SimpleNamespace(commit=AsyncMock())

        with self.assertRaises(ValueError):
            asyncio.run(
                service.mark_completed(
                    session,
                    "upload-1",
                    "task-1",
                    {
                        "chunks_indexed": 1,
                        "nodes_upserted": 0,
                        "relationships_upserted": 0,
                    },
                )
            )

        session.commit.assert_not_awaited()


class ReadyContextTests(unittest.TestCase):
    def test_course_without_ready_documents_is_rejected(self):
        service = CourseService()
        course = SimpleNamespace(id="course-uuid", display_name="CYBER")
        service.resolve = AsyncMock(return_value=course)
        result = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))
        session = SimpleNamespace(execute=AsyncMock(return_value=result))

        with self.assertRaises(CourseNotReadyError):
            asyncio.run(service.get_ready_context(session, "cyber"))

    def test_course_summary_excludes_historical_duplicate_hashes(self):
        service = CourseService()
        now = datetime.now(timezone.utc)
        course = SimpleNamespace(id="course-uuid", display_name="CYBER")
        documents = [
            SimpleNamespace(
                upload_id="new",
                course_uuid=course.id,
                content_hash="same-hash",
                status="ready",
                stage=ProcessingStage.READY.value,
                updated_at=now,
                processed_chunk_count=7,
                graph_node_count=18,
                graph_edge_count=14,
            ),
            SimpleNamespace(
                upload_id="old",
                course_uuid=course.id,
                content_hash="same-hash",
                status="ready",
                stage=ProcessingStage.READY.value,
                updated_at=now,
                processed_chunk_count=7,
                graph_node_count=18,
                graph_edge_count=14,
            ),
        ]
        courses_result = SimpleNamespace(scalars=lambda: [course])
        documents_result = SimpleNamespace(scalars=lambda: documents)
        session = SimpleNamespace(
            execute=AsyncMock(side_effect=[courses_result, documents_result])
        )

        summaries = asyncio.run(service.list_summaries(session))

        self.assertEqual(summaries[0].total_documents, 1)
        self.assertEqual(summaries[0].ready_documents, 1)
        self.assertEqual(summaries[0].processed_chunk_count, 7)
        self.assertEqual(summaries[0].duplicate_records, 1)

    def test_course_metrics_exclude_failed_document_artifacts(self):
        service = CourseService()
        now = datetime.now(timezone.utc)
        course = SimpleNamespace(id="course-uuid", display_name="CYBER")
        failed = SimpleNamespace(
            upload_id="failed",
            course_uuid=course.id,
            content_hash="failed-hash",
            status="failed",
            stage=ProcessingStage.FAILED.value,
            updated_at=now,
            processed_chunk_count=7,
            graph_node_count=18,
            graph_edge_count=14,
        )
        courses_result = SimpleNamespace(scalars=lambda: [course])
        documents_result = SimpleNamespace(scalars=lambda: [failed])
        session = SimpleNamespace(
            execute=AsyncMock(side_effect=[courses_result, documents_result])
        )

        summary = asyncio.run(service.list_summaries(session))[0]

        self.assertEqual(summary.failed_documents, 1)
        self.assertEqual(summary.ready_documents, 0)
        self.assertEqual(summary.processed_chunk_count, 0)
        self.assertEqual(summary.graph_node_count, 0)
        self.assertEqual(summary.graph_edge_count, 0)


class RetryEndpointTests(unittest.TestCase):
    def test_full_queue_defers_retry_without_marking_it_failed(self):
        with tempfile.NamedTemporaryFile(suffix=".pdf") as source:
            record = SimpleNamespace(
                upload_id="upload-1",
                task_id="old-task",
                course_uuid="course-uuid",
                course_id="CYBER",
                original_filename="course.pdf",
                storage_key=str(Path(source.name)),
                stage="FAILED",
                retryable=True,
            )
            retried_record = SimpleNamespace(**vars(record))
            retried_record.task_id = "new-task"
            retried_record.stage = "UPLOADED"
            retried_record.retryable = False

            with (
                patch.object(ingest.upload_service, "get_upload", AsyncMock(return_value=record)),
                patch.object(
                    ingest.upload_service,
                    "retry_upload",
                    AsyncMock(return_value=retried_record),
                ),
                patch.object(ingest.upload_service, "mark_failed", AsyncMock()) as mark_failed,
                patch.object(
                    ingest.processing_coordinator,
                    "submit",
                    AsyncMock(return_value=EnqueueDisposition.DEFERRED),
                ),
                patch.object(ingest.storage_service, "exists", return_value=True),
            ):
                response = asyncio.run(
                    ingest.retry_upload(record.upload_id, SimpleNamespace())
                )

            self.assertEqual(response.task_id, "new-task")
            self.assertIn("capacity", response.message)
            mark_failed.assert_not_awaited()


class ProcessingCoordinatorTests(unittest.TestCase):
    def test_bounded_queue_defers_when_capacity_is_full(self):
        config = Settings(
            _env_file=None,
            PROCESSING_CONCURRENCY=1,
            PROCESSING_QUEUE_CAPACITY=1,
        )
        coordinator = ProcessingCoordinator(
            config=config,
            upload_service=SimpleNamespace(),
            processing_service=SimpleNamespace(),
        )
        coordinator._started = True

        async def submit_jobs():
            first = await coordinator.submit("upload-1", "task-1")
            duplicate = await coordinator.submit("upload-1", "task-1")
            second = await coordinator.submit("upload-2", "task-2")
            return first, duplicate, second

        first, duplicate, second = asyncio.run(submit_jobs())

        self.assertEqual(first, EnqueueDisposition.ACCEPTED)
        self.assertEqual(duplicate, EnqueueDisposition.ALREADY_QUEUED)
        self.assertEqual(second, EnqueueDisposition.DEFERRED)
        self.assertEqual(coordinator.queue_depth, 1)

    def test_default_configuration_starts_one_processor(self):
        config = Settings(
            _env_file=None,
            PROCESSING_CONCURRENCY=1,
            PROCESSING_QUEUE_CAPACITY=2,
        )
        coordinator = ProcessingCoordinator(
            config=config,
            upload_service=SimpleNamespace(),
            processing_service=SimpleNamespace(),
        )
        coordinator._recover_and_fill = AsyncMock()

        async def exercise():
            await coordinator.start()
            worker_count = len(coordinator._workers)
            await coordinator.stop()
            return worker_count

        self.assertEqual(asyncio.run(exercise()), 1)


class DocumentProcessingServiceTests(unittest.TestCase):
    def _service_fixture(self):
        record = SimpleNamespace(
            upload_id="upload-1",
            task_id="task-1",
            lease_owner="instance-1",
            status="active",
            course_uuid="course-1",
            course_id="CYBER",
            original_filename="course.pdf",
            storage_key="courses/course-1/documents/hash.pdf",
        )
        chunk = DocumentChunk(
            id="upload-1:1:0",
            text="Course content",
            metadata={
                "upload_id": "upload-1",
                "document_id": "course-1",
                "page_number": 1,
            },
        )
        upload_service = SimpleNamespace(
            get_upload=AsyncMock(return_value=record),
            heartbeat=AsyncMock(return_value=True),
            set_stage=AsyncMock(return_value=True),
            mark_completed=AsyncMock(return_value=True),
            mark_failed=AsyncMock(return_value=True),
            release_lease=AsyncMock(return_value=True),
        )
        parser_service = SimpleNamespace(
            extract_pages_from_bytes=Mock(return_value=[(1, "Course content")]),
            chunk_pages=Mock(return_value=[chunk]),
        )
        graph = GraphExtractionResponse(
            nodes=[ConceptNode(id="concept", name="Concept", type="topic")],
            relationships=[],
        )
        ingestion_service = SimpleNamespace(
            cleanup_upload=AsyncMock(),
            upsert_chunks_to_qdrant=Mock(return_value=1),
            extract_graph_from_chunks=AsyncMock(return_value=graph),
            store_graph_extraction=AsyncMock(),
        )
        config = Settings(
            _env_file=None,
            PROCESSING_HEARTBEAT_SECONDS=30,
            PROCESSING_LEASE_SECONDS=180,
        )
        service = DocumentProcessingService(
            config=config,
            ingestion_service=ingestion_service,
            parser_service=parser_service,
            upload_service=upload_service,
        )
        fake_session = SimpleNamespace()

        async def run_with_session(operation):
            return await operation(fake_session)

        service._run_with_session = run_with_session
        return service, upload_service, ingestion_service, chunk

    def test_shared_processor_preserves_all_durable_stage_transitions(self):
        service, upload_service, ingestion_service, chunk = self._service_fixture()

        with (
            patch(
                "app.services.document_processing_service.storage_service.get_bytes",
                return_value=b"%PDF-test",
            ),
            patch(
                "app.services.document_processing_service.storage_service.exists",
                return_value=True,
            ),
        ):
            result = asyncio.run(
                service.process_document(
                    "upload-1", "task-1", lease_owner="instance-1"
                )
            )

        self.assertEqual(result["status"], "ready")
        stages = [call.args[3] for call in upload_service.set_stage.await_args_list]
        self.assertEqual(
            stages,
            [
                ProcessingStage.EXTRACTING,
                ProcessingStage.EXTRACTED,
                ProcessingStage.CHUNKING,
                ProcessingStage.CHUNKED,
                ProcessingStage.EMBEDDING,
                ProcessingStage.EMBEDDED,
                ProcessingStage.BUILDING_GRAPH,
                ProcessingStage.GRAPH_BUILT,
            ],
        )
        self.assertEqual(chunk.metadata["execution_token"], "task-1")
        ingestion_service.cleanup_upload.assert_awaited_once_with(
            "upload-1", "course-1"
        )
        graph_call = ingestion_service.store_graph_extraction.await_args
        self.assertEqual(graph_call.kwargs["execution_token"], "task-1")
        upload_service.mark_completed.assert_awaited_once()

    def test_processing_failure_cleans_partial_outputs_and_marks_failed(self):
        service, upload_service, ingestion_service, _ = self._service_fixture()
        ingestion_service.upsert_chunks_to_qdrant.side_effect = RuntimeError(
            "Qdrant connection failed"
        )

        with patch(
            "app.services.document_processing_service.storage_service.get_bytes",
            return_value=b"%PDF-test",
        ):
            result = asyncio.run(
                service.process_document(
                    "upload-1", "task-1", lease_owner="instance-1"
                )
            )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(ingestion_service.cleanup_upload.await_count, 2)
        upload_service.mark_failed.assert_awaited_once()


class ProcessingLeaseTests(unittest.TestCase):
    def test_valid_foreign_lease_cannot_be_claimed(self):
        service = UploadService()
        record = SimpleNamespace(
            status="active",
            stage=ProcessingStage.UPLOADED.value,
            lease_owner="other-instance",
            lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=2),
        )
        result = SimpleNamespace(scalar_one_or_none=lambda: record)
        session = SimpleNamespace(
            execute=AsyncMock(return_value=result),
            rollback=AsyncMock(),
        )

        claimed = asyncio.run(
            service.claim_for_processing(
                session,
                upload_id="upload-1",
                task_id="task-1",
                lease_owner="this-instance",
                lease_seconds=180,
            )
        )

        self.assertIsNone(claimed)
        session.rollback.assert_awaited_once()

    def test_expired_lease_is_claimed_and_extended(self):
        service = UploadService()
        record = SimpleNamespace(
            status="active",
            stage=ProcessingStage.UPLOADED.value,
            lease_owner="old-instance",
            lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
            last_heartbeat_at=None,
        )
        attempt = SimpleNamespace(last_heartbeat_at=None)
        result = SimpleNamespace(scalar_one_or_none=lambda: record)
        session = SimpleNamespace(
            execute=AsyncMock(return_value=result),
            commit=AsyncMock(),
            refresh=AsyncMock(),
        )
        service._current_attempt = AsyncMock(return_value=attempt)

        claimed = asyncio.run(
            service.claim_for_processing(
                session,
                upload_id="upload-1",
                task_id="task-1",
                lease_owner="new-instance",
                lease_seconds=180,
            )
        )

        self.assertIs(claimed, record)
        self.assertEqual(record.lease_owner, "new-instance")
        self.assertGreater(record.lease_expires_at, datetime.now(timezone.utc))
        session.commit.assert_awaited_once()

    def test_interrupted_execution_creates_a_real_recovery_attempt(self):
        service = UploadService()
        record = SimpleNamespace(
            upload_id="upload-1",
            task_id="old-task",
            status="active",
            stage=ProcessingStage.EMBEDDED.value,
            attempt_count=1,
            failure_category=None,
            retryable=False,
            error_message=None,
            result_json={"partial": True},
            last_attempted_at=None,
            last_heartbeat_at=None,
            started_at=datetime.now(timezone.utc),
            completed_at=None,
            lease_owner="dead-instance",
            lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        )
        previous_attempt = SimpleNamespace(
            stage=ProcessingStage.EMBEDDED.value,
            failure_category=None,
            retryable=False,
            error_message=None,
            completed_at=None,
            last_heartbeat_at=None,
        )
        result = SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: [record])
        )
        session = SimpleNamespace(
            execute=AsyncMock(return_value=result),
            add=Mock(),
            commit=AsyncMock(),
        )
        service._current_attempt = AsyncMock(return_value=previous_attempt)

        recovered = asyncio.run(service.prepare_stale_recoveries(session))

        self.assertEqual(recovered, [record])
        self.assertEqual(record.attempt_count, 2)
        self.assertNotEqual(record.task_id, "old-task")
        self.assertEqual(record.stage, ProcessingStage.UPLOADED.value)
        self.assertIsNone(record.lease_owner)
        self.assertEqual(previous_attempt.stage, ProcessingStage.FAILED.value)
        self.assertEqual(
            previous_attempt.failure_category,
            FailureCategory.WORKER_ERROR.value,
        )
        session.add.assert_called_once()
        session.commit.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
