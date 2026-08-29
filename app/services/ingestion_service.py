import asyncio
import re
from collections.abc import Sequence

from groq import BadRequestError, Groq
from neo4j import AsyncDriver
from qdrant_client import QdrantClient
from qdrant_client.http.exceptions import UnexpectedResponse
from qdrant_client.models import (
    Distance,
    Document,
    FieldCondition,
    Filter,
    FilterSelector,
    MatchValue,
    PayloadSchemaType,
    PointStruct,
    VectorParams,
)

from app.core.config import settings
from app.core.database import neo4j_driver, qdrant_client
from app.core.exceptions import LLMConfigurationError
from app.schemas.extraction import GraphExtractionResponse
from app.services.parser_service import DocumentChunk


class IngestionService:
    def __init__(
        self,
        graph_driver: AsyncDriver = neo4j_driver,
        vector_client: QdrantClient = qdrant_client,
    ) -> None:
        self.graph_driver = graph_driver
        self.vector_client = vector_client
        self.collection_name = settings.qdrant_collection_name
        self._embedding_model = None

    def upsert_chunks_to_qdrant(self, chunks: Sequence[DocumentChunk]) -> int:
        if not chunks:
            return 0

        if settings.embedding_provider == "qdrant_cloud":
            self._ensure_qdrant_collection(vector_size=settings.embedding_dimension)
            vectors = [
                Document(text=chunk.text, model=settings.embedding_model_name)
                for chunk in chunks
            ]
        else:
            embeddings = self.embedding_model.encode(
                [chunk.text for chunk in chunks],
                convert_to_numpy=True,
                normalize_embeddings=True,
                show_progress_bar=False,
            )
            self._ensure_qdrant_collection(vector_size=len(embeddings[0]))
            vectors = [embedding.tolist() for embedding in embeddings]

        points = [
            PointStruct(
                id=self._qdrant_point_id(chunk.id),
                vector=vector,
                payload={
                    **chunk.metadata,
                    "text": chunk.text,
                },
            )
            for chunk, vector in zip(chunks, vectors, strict=True)
        ]
        self.vector_client.upsert(collection_name=self.collection_name, points=points)
        return len(points)

    @property
    def embedding_model(self):
        if self._embedding_model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:
                raise RuntimeError(
                    "Local embeddings require requirements-local-models.txt."
                ) from exc

            self._embedding_model = SentenceTransformer(
                settings.embedding_model_name,
                device=self._resolve_embedding_device(),
            )
        return self._embedding_model

    async def extract_graph_from_chunks(
        self,
        chunks: Sequence[DocumentChunk],
    ) -> GraphExtractionResponse:
        if not chunks:
            return GraphExtractionResponse()

        # One representative request avoids rate-limit failures from making an
        # LLM call for every vector chunk. All chunks are still indexed for search.
        sample_count = min(8, len(chunks))
        sample_indexes = {
            round(index * (len(chunks) - 1) / max(sample_count - 1, 1))
            for index in range(sample_count)
        }
        context = "\n\n".join(
            f"[Document excerpt {index + 1}]\n{chunks[index].text[:700]}"
            for index in sorted(sample_indexes)
        )
        return await self.extract_graph_from_text(context)

    async def extract_graph_from_text(self, text: str) -> GraphExtractionResponse:
        provider = settings.llm_provider.lower()
        if provider == "gemini":
            return await asyncio.to_thread(self._extract_with_gemini, text)
        if provider == "groq":
            return await asyncio.to_thread(self._extract_with_groq, text)
        raise ValueError(f"Unsupported LLM_PROVIDER: {settings.llm_provider}")

    async def store_graph_extraction(
        self,
        extraction: GraphExtractionResponse,
        course_id: str,
        *,
        upload_id: str,
        document_name: str,
        course_name: str = "",
        execution_token: str = "",
    ) -> None:
        async with self.graph_driver.session() as session:
            await session.run(
                """
                MERGE (course:Course {id: $course_id})
                SET course.name = $course_name,
                    course.updated_at = datetime()
                """,
                course_id=course_id,
                course_name=course_name,
            )

            for node in extraction.nodes:
                await session.run(
                    """
                    MERGE (c:Concept {id: $id})
                    SET c.name = $name,
                        c.type = $type,
                        c.description = $description,
                        c.source_id = $source_id,
                        c.course_id = $course_id,
                        c.upload_id = $upload_id,
                        c.execution_token = $execution_token,
                        c.document_name = $document_name
                    WITH c
                    MATCH (course:Course {id: $course_id})
                    MERGE (course)-[:CONTAINS]->(c)
                    """,
                    id=self._scoped_concept_id(course_id, upload_id, node.id),
                    source_id=node.id,
                    name=node.name,
                    type=node.type,
                    description=node.description,
                    course_id=course_id,
                    upload_id=upload_id,
                    execution_token=execution_token,
                    document_name=document_name,
                )

            for relationship in extraction.relationships:
                relation_type = self._safe_relationship_type(relationship.relation_type)
                await session.run(
                    f"""
                    MATCH (course:Course {{id: $course_id}})
                    MATCH (source:Concept {{id: $source_node_id}})
                    MATCH (target:Concept {{id: $target_node_id}})
                    MATCH (course)-[:CONTAINS]->(source)
                    MATCH (course)-[:CONTAINS]->(target)
                    MERGE (source)-[r:{relation_type} {{course_id: $course_id}}]->(target)
                    SET r.relation_type = $relation_type,
                        r.course_id = $course_id,
                        r.upload_id = $upload_id,
                        r.execution_token = $execution_token,
                        r.document_name = $document_name
                    """,
                    course_id=course_id,
                    source_node_id=self._scoped_concept_id(
                        course_id,
                        upload_id,
                        relationship.source_node_id,
                    ),
                    target_node_id=self._scoped_concept_id(
                        course_id,
                        upload_id,
                        relationship.target_node_id,
                    ),
                    relation_type=relationship.relation_type,
                    upload_id=upload_id,
                    execution_token=execution_token,
                    document_name=document_name,
                )

    async def ingest_chunks(self, chunks: Sequence[DocumentChunk]) -> dict[str, int]:
        self._validate_llm_configured()
        course_id = self._course_id_from_chunks(chunks)
        graph_extraction = await self.extract_graph_from_chunks(chunks)
        await self.store_graph_extraction(
            graph_extraction,
            course_id=course_id,
            upload_id=str(chunks[0].metadata.get("upload_id", "")),
            document_name=str(chunks[0].metadata.get("document_name", "")),
            execution_token=str(chunks[0].metadata.get("execution_token", "")),
        )
        vector_count = self.upsert_chunks_to_qdrant(chunks)
        return {
            "chunks_indexed": vector_count,
            "nodes_upserted": len(graph_extraction.nodes),
            "relationships_upserted": len(graph_extraction.relationships),
        }

    async def cleanup_upload(self, upload_id: str, course_id: str) -> None:
        if self._collection_exists_for_cleanup():
            self.vector_client.delete(
                collection_name=self.collection_name,
                points_selector=FilterSelector(
                    filter=Filter(
                        must=[FieldCondition(key="upload_id", match=MatchValue(value=upload_id))]
                    )
                ),
                wait=True,
            )
        async with self.graph_driver.session() as session:
            await session.run(
                """
                MATCH (course:Course {id: $course_id})-[:CONTAINS]->(concept:Concept {upload_id: $upload_id})
                DETACH DELETE concept
                """,
                course_id=course_id,
                upload_id=upload_id,
            )
            await session.run(
                """
                MATCH (course:Course {id: $course_id})
                WHERE NOT (course)-[:CONTAINS]->(:Concept)
                DETACH DELETE course
                """,
                course_id=course_id,
            )

    def _collection_exists_for_cleanup(self) -> bool:
        try:
            return bool(self.vector_client.collection_exists(self.collection_name))
        except UnexpectedResponse as exc:
            if exc.status_code == 404:
                return False
            raise
        except AttributeError:
            try:
                self.vector_client.get_collection(self.collection_name)
            except UnexpectedResponse as exc:
                if exc.status_code == 404:
                    return False
                raise
            return True

    def _ensure_qdrant_collection(self, vector_size: int) -> None:
        if vector_size != settings.embedding_dimension:
            raise RuntimeError(
                f"Embedding model returned {vector_size} dimensions; "
                f"EMBEDDING_DIMENSION is {settings.embedding_dimension}."
            )
        existing_collections = self.vector_client.get_collections().collections
        collection_exists = any(
            collection.name == self.collection_name
            for collection in existing_collections
        )
        if not collection_exists:
            try:
                self.vector_client.create_collection(
                    collection_name=self.collection_name,
                    vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE),
                )
            except UnexpectedResponse as exc:
                if exc.status_code != 409 and b"already exists" not in exc.content.lower():
                    raise
        else:
            self.validate_qdrant_collection()

        self._ensure_qdrant_payload_indexes()

    def validate_qdrant_collection(self) -> None:
        """Reject an existing collection created for incompatible embeddings."""

        if not self.vector_client.collection_exists(self.collection_name):
            return
        collection = self.vector_client.get_collection(self.collection_name)
        vectors = collection.config.params.vectors
        size = getattr(vectors, "size", None)
        if size is None:
            raise RuntimeError(
                "Qdrant collection uses named or unsupported vectors; configure a new "
                "QDRANT_COLLECTION_NAME for this embedding model."
            )
        if int(size) != settings.embedding_dimension:
            raise RuntimeError(
                f"Qdrant collection dimension is {size}, but the configured embedding "
                f"dimension is {settings.embedding_dimension}. Use a new collection name; "
                "old vectors are incompatible."
            )

    def _ensure_qdrant_payload_indexes(self) -> None:
        collection = self.vector_client.get_collection(self.collection_name)
        payload_schema = collection.payload_schema or {}
        if "upload_id" in payload_schema:
            return
        try:
            self.vector_client.create_payload_index(
                collection_name=self.collection_name,
                field_name="upload_id",
                field_schema=PayloadSchemaType.KEYWORD,
                wait=True,
            )
        except UnexpectedResponse as exc:
            if exc.status_code != 409 and b"already exists" not in exc.content.lower():
                raise

    def _extract_with_groq(self, text: str) -> GraphExtractionResponse:
        if not settings.groq_api_key:
            raise LLMConfigurationError("GROQ_API_KEY is required when LLM_PROVIDER=groq")

        client = Groq(
            api_key=settings.groq_api_key,
            timeout=settings.provider_timeout_seconds,
        )
        contexts = self._graph_extraction_contexts(text)
        for attempt, context in enumerate(contexts):
            try:
                completion = client.chat.completions.create(
                    model=settings.groq_model,
                    messages=[
                        {
                            "role": "system",
                            "content": self._extraction_system_prompt(),
                        },
                        {
                            "role": "user",
                            "content": context,
                        },
                    ],
                    temperature=0,
                    response_format={
                        "type": "json_schema",
                        "json_schema": {
                            "name": "concept_graph_extraction",
                            "strict": True,
                            "schema": self._graph_extraction_schema(),
                        },
                    },
                )
                content = completion.choices[0].message.content or "{}"
                return GraphExtractionResponse.model_validate_json(content)
            except BadRequestError as exc:
                if not self._is_json_validation_failure(exc) or attempt == len(contexts) - 1:
                    raise

        raise RuntimeError("Graph extraction exhausted its provider attempts.")

    @staticmethod
    def _graph_extraction_contexts(text: str) -> tuple[str, ...]:
        contexts: list[str] = []
        excerpts = [excerpt.strip() for excerpt in text.split("\n\n") if excerpt.strip()]
        for limit in (1800, 1200, 800):
            per_excerpt = max(80, limit // max(1, len(excerpts)))
            shortened = "\n\n".join(
                excerpt[:per_excerpt].rsplit(" ", 1)[0].strip()
                for excerpt in excerpts
            ).strip()
            if shortened and (not contexts or shortened != contexts[-1]):
                contexts.append(shortened)
        return tuple(contexts or [text])

    @staticmethod
    def _is_json_validation_failure(exc: BadRequestError) -> bool:
        body = exc.body
        if isinstance(body, dict):
            error = body.get("error")
            if isinstance(error, dict) and error.get("code") == "json_validate_failed":
                return True
        return "json_validate_failed" in str(exc).lower()

    def _extract_with_gemini(self, text: str) -> GraphExtractionResponse:
        if not settings.gemini_api_key:
            raise LLMConfigurationError("GEMINI_API_KEY is required when LLM_PROVIDER=gemini")

        import google.generativeai as genai

        genai.configure(api_key=settings.gemini_api_key)
        model = genai.GenerativeModel(settings.gemini_model)
        response = model.generate_content(
            [
                self._extraction_system_prompt(),
                text,
            ],
            generation_config={
                "temperature": 0,
                "response_mime_type": "application/json",
            },
            request_options={"timeout": settings.provider_timeout_seconds},
        )
        return GraphExtractionResponse.model_validate_json(response.text or "{}")

    @staticmethod
    def _extraction_system_prompt() -> str:
        return (
            "Extract academic concepts and prerequisite relationships from the text. "
            "Return only the requested structured response. Use stable lowercase snake_case "
            "ids for nodes. Use uppercase snake_case relationship types such as "
            "PREREQUISITE_OF, PART_OF, EXPLAINS, or RELATED_TO. Every relationship "
            "endpoint must reference an extracted node."
        )

    @staticmethod
    def _graph_extraction_schema() -> dict[str, object]:
        # Groq strict mode requires every property to be required and every
        # object to reject additional properties.
        concept = {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "name": {"type": "string"},
                "type": {"type": "string"},
                "description": {"type": "string"},
            },
            "required": ["id", "name", "type", "description"],
            "additionalProperties": False,
        }
        relationship = {
            "type": "object",
            "properties": {
                "source_node_id": {"type": "string"},
                "target_node_id": {"type": "string"},
                "relation_type": {"type": "string"},
            },
            "required": ["source_node_id", "target_node_id", "relation_type"],
            "additionalProperties": False,
        }
        return {
            "type": "object",
            "properties": {
                "nodes": {"type": "array", "items": concept},
                "relationships": {"type": "array", "items": relationship},
            },
            "required": ["nodes", "relationships"],
            "additionalProperties": False,
        }

    @staticmethod
    def _resolve_embedding_device() -> str:
        import torch

        if torch.backends.mps.is_available():
            return "mps"
        return "cpu"

    @staticmethod
    def _validate_llm_configured() -> None:
        provider = settings.llm_provider.lower()
        if provider == "groq" and not settings.groq_api_key:
            raise LLMConfigurationError("GROQ_API_KEY is required when LLM_PROVIDER=groq")
        if provider == "gemini" and not settings.gemini_api_key:
            raise LLMConfigurationError("GEMINI_API_KEY is required when LLM_PROVIDER=gemini")

    @staticmethod
    def _safe_relationship_type(relation_type: str) -> str:
        normalized = re.sub(r"[^A-Za-z0-9_]", "_", relation_type.upper()).strip("_")
        if not normalized:
            return "RELATED_TO"
        if normalized[0].isdigit():
            return f"RELATED_{normalized}"
        return normalized

    @staticmethod
    def _qdrant_point_id(chunk_id: str) -> str:
        import uuid

        return str(uuid.uuid5(uuid.NAMESPACE_URL, chunk_id))

    @staticmethod
    def _course_id_from_chunks(chunks: Sequence[DocumentChunk]) -> str:
        if not chunks:
            raise ValueError("Cannot ingest an empty chunk list.")

        course_id = chunks[0].metadata.get("document_id")
        if not isinstance(course_id, str) or not course_id.strip():
            raise ValueError("Chunk metadata must include a non-empty document_id.")
        return course_id

    @staticmethod
    def _scoped_concept_id(course_id: str, upload_id: str, concept_id: str) -> str:
        return f"{course_id}:{upload_id}:{concept_id}"
