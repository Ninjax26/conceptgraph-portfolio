import asyncio
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass

from groq import BadRequestError, Groq
from neo4j import AsyncDriver
from pydantic import ValidationError
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
from app.core.exceptions import GraphStructureError, LLMConfigurationError
from app.schemas.extraction import (
    ALLOWED_RELATIONSHIP_TYPES,
    ConceptNode,
    ConceptRelationship,
    GraphExtractionResponse,
    normalize_concept_name,
)
from app.services.parser_service import DocumentChunk


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class GraphSectionBatch:
    section_key: str
    section_label: str
    chunks: tuple[DocumentChunk, ...]


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

        batches = self._graph_section_batches(chunks)
        section_labels = {
            batch.section_key: batch.section_label
            for batch in batches
        }
        represented_sections: set[str] = set()
        successful_batches = 0
        failed_batches = 0
        partials: list[GraphExtractionResponse] = []

        for batch in batches:
            context = "\n\n".join(
                self._format_graph_excerpt(chunk)
                for chunk in batch.chunks
            )
            try:
                extraction = await self.extract_graph_from_text(context)
            except GraphStructureError:
                failed_batches += 1
                logger.warning(
                    "Skipped one malformed graph batch for section %s",
                    batch.section_label,
                )
                continue

            successful_batches += 1
            enriched = self._attach_provenance(extraction, batch.chunks)
            if enriched.nodes:
                represented_sections.add(batch.section_key)
            partials.append(enriched)

        failed_sections = set(section_labels) - represented_sections
        return self._merge_graph_extractions(
            partials,
            sections_total=len(section_labels),
            sections_succeeded=len(represented_sections),
            sections_failed=len(section_labels) - len(represented_sections),
            batches_total=len(batches),
            batches_succeeded=successful_batches,
            batches_failed=failed_batches,
            failed_section_labels=sorted(
                section_labels[key]
                for key in failed_sections
            ),
        )

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
                        c.normalized_name = $normalized_name,
                        c.type = $type,
                        c.description = $description,
                        c.source_id = $source_id,
                        c.source_chunk_id = $source_chunk_id,
                        c.course_id = $course_id,
                        c.upload_id = $upload_id,
                        c.execution_token = $execution_token,
                        c.document_name = $document_name,
                        c.page_number = $page_number,
                        c.section_heading = $section_heading,
                        c.source_chunk_ids = $source_chunk_ids,
                        c.page_numbers = $page_numbers,
                        c.section_headings = $section_headings
                    WITH c
                    MATCH (course:Course {id: $course_id})
                    MERGE (course)-[:CONTAINS]->(c)
                    """,
                    id=self._scoped_concept_id(course_id, upload_id, node.id),
                    source_id=node.id,
                    name=node.name,
                    normalized_name=node.normalized_name,
                    type=node.type,
                    description=node.description,
                    source_chunk_id=node.source_chunk_id,
                    course_id=course_id,
                    upload_id=node.upload_id or upload_id,
                    execution_token=execution_token,
                    document_name=node.document_name or document_name,
                    page_number=node.page_number,
                    section_heading=node.section_heading,
                    source_chunk_ids=node.source_chunk_ids,
                    page_numbers=node.page_numbers,
                    section_headings=node.section_headings,
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
                if not self._is_json_validation_failure(exc):
                    raise
            except ValidationError:
                pass

        # Some compatible models reject strict JSON Schema even when they can
        # return valid JSON. Use the smallest context for one simpler fallback,
        # then still validate the graph locally before accepting it.
        try:
            completion = client.chat.completions.create(
                model=settings.groq_model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            f"{self._extraction_system_prompt()} Return one JSON object "
                            "with exactly two arrays named nodes and relationships."
                        ),
                    },
                    {
                        "role": "user",
                        "content": contexts[-1],
                    },
                ],
                temperature=0,
                response_format={"type": "json_object"},
            )
            content = completion.choices[0].message.content or "{}"
            return GraphExtractionResponse.model_validate_json(content)
        except (BadRequestError, ValidationError, ValueError) as exc:
            raise GraphStructureError(
                "json_validate_failed: Groq could not produce a valid graph for this section."
            ) from exc

    @staticmethod
    def _graph_extraction_contexts(text: str) -> tuple[str, ...]:
        contexts: list[str] = []
        excerpts = [excerpt.strip() for excerpt in text.split("\n\n") if excerpt.strip()]
        for limit in (16000, 8000, 4000):
            per_excerpt = max(160, limit // max(1, len(excerpts)))
            shortened = "\n\n".join(
                IngestionService._truncate_excerpt(excerpt, per_excerpt)
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
        try:
            return GraphExtractionResponse.model_validate_json(response.text or "{}")
        except ValidationError as exc:
            raise GraphStructureError(
                "json_validate_failed: Gemini could not produce a valid graph for this section."
            ) from exc

    @staticmethod
    def _extraction_system_prompt() -> str:
        relationship_types = ", ".join(sorted(ALLOWED_RELATIONSHIP_TYPES))
        return (
            "Extract academic concepts and relationships from the supplied source excerpts. "
            "Return only the requested structured response. Use stable lowercase snake_case "
            "ids for nodes. Every concept must cite exactly one supplied Source chunk ID in "
            "source_chunk_id; never invent a source ID. Every relationship endpoint must "
            "reference an extracted node. The only permitted relationship types are: "
            f"{relationship_types}."
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
                "source_chunk_id": {"type": "string"},
            },
            "required": ["id", "name", "type", "description", "source_chunk_id"],
            "additionalProperties": False,
        }
        relationship = {
            "type": "object",
            "properties": {
                "source_node_id": {"type": "string"},
                "target_node_id": {"type": "string"},
                "relation_type": {
                    "type": "string",
                    "enum": sorted(ALLOWED_RELATIONSHIP_TYPES),
                },
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
        if normalized not in ALLOWED_RELATIONSHIP_TYPES:
            raise ValueError(f"Unsupported graph relationship type: {relation_type}")
        return normalized

    @staticmethod
    def _sample_graph_chunks(chunks: Sequence[DocumentChunk]) -> list[DocumentChunk]:
        """Cover each section, always including its beginning, middle, and end."""

        sections: dict[tuple[str, str], list[DocumentChunk]] = {}
        for chunk in chunks:
            heading = str(chunk.metadata.get("section_heading") or "").strip()
            page = str(chunk.metadata.get("page_number") or "unknown")
            section_key = heading.casefold() if heading else f"page:{page}"
            document = str(chunk.metadata.get("document_name") or "")
            sections.setdefault((document, section_key), []).append(chunk)

        sampled: list[DocumentChunk] = []
        for section_chunks in sections.values():
            sample_count = min(9, len(section_chunks))
            positions = sorted(
                {
                    round(index * (len(section_chunks) - 1) / max(sample_count - 1, 1))
                    for index in range(sample_count)
                }
            )
            sampled.extend(section_chunks[position] for position in positions)
        return sampled

    @classmethod
    def _graph_section_batches(
        cls,
        chunks: Sequence[DocumentChunk],
        *,
        batch_size: int = 3,
    ) -> list[GraphSectionBatch]:
        """Create bounded requests while preserving each document section."""

        sections: dict[str, tuple[str, list[DocumentChunk]]] = {}
        for chunk in cls._sample_graph_chunks(chunks):
            document = str(chunk.metadata.get("document_name") or "unknown")
            heading = str(chunk.metadata.get("section_heading") or "").strip()
            page = chunk.metadata.get("page_number")
            section_identity = heading.casefold() if heading else f"page:{page or 'unknown'}"
            section_key = f"{document.casefold()}::{section_identity}"
            section_label = heading or f"Page {page or 'unknown'}"
            sections.setdefault(section_key, (section_label, []))[1].append(chunk)

        batches: list[GraphSectionBatch] = []
        for section_key, (section_label, section_chunks) in sections.items():
            for offset in range(0, len(section_chunks), batch_size):
                batches.append(
                    GraphSectionBatch(
                        section_key=section_key,
                        section_label=section_label,
                        chunks=tuple(section_chunks[offset : offset + batch_size]),
                    )
                )
        return batches

    @staticmethod
    def _format_graph_excerpt(chunk: DocumentChunk) -> str:
        metadata = chunk.metadata
        return (
            f"[Source chunk ID: {chunk.id} | "
            f"PDF: {metadata.get('document_name') or 'unknown'} | "
            f"Page: {metadata.get('page_number') or 'unknown'} | "
            f"Section: {metadata.get('section_heading') or 'Unlabelled'}]\n"
            f"{chunk.text}"
        )

    @staticmethod
    def _truncate_excerpt(excerpt: str, limit: int) -> str:
        if len(excerpt) <= limit:
            return excerpt
        header, separator, body = excerpt.partition("\n")
        if not separator or len(header) >= limit:
            return header
        body_limit = limit - len(header) - len(separator)
        shortened_body = body[:body_limit]
        shortened_body = shortened_body.rsplit(" ", 1)[0].strip() or shortened_body.strip()
        return f"{header}\n{shortened_body}" if shortened_body else header

    @staticmethod
    def _attach_provenance(
        extraction: GraphExtractionResponse,
        sampled_chunks: Sequence[DocumentChunk],
    ) -> GraphExtractionResponse:
        chunks_by_id = {chunk.id: chunk for chunk in sampled_chunks}
        nodes: list[ConceptNode] = []
        for node in extraction.nodes:
            source = chunks_by_id.get(node.source_chunk_id)
            if source is None:
                continue
            metadata = source.metadata
            page_number = metadata.get("page_number")
            nodes.append(
                node.model_copy(
                    update={
                        "document_name": str(metadata.get("document_name") or ""),
                        "page_number": page_number if isinstance(page_number, int) else None,
                        "section_heading": str(
                            metadata.get("section_heading") or "Unlabelled section"
                        ),
                        "upload_id": str(metadata.get("upload_id") or ""),
                        "source_chunk_ids": [node.source_chunk_id],
                        "page_numbers": (
                            [page_number] if isinstance(page_number, int) else []
                        ),
                        "section_headings": [
                            str(metadata.get("section_heading") or "Unlabelled section")
                        ],
                    }
                )
            )

        retained_ids = {node.id for node in nodes}
        relationships = [
            relationship
            for relationship in extraction.relationships
            if relationship.source_node_id in retained_ids
            and relationship.target_node_id in retained_ids
        ]
        return GraphExtractionResponse(nodes=nodes, relationships=relationships)

    @staticmethod
    def _merge_graph_extractions(
        extractions: Sequence[GraphExtractionResponse],
        *,
        sections_total: int,
        sections_succeeded: int,
        sections_failed: int,
        batches_total: int,
        batches_succeeded: int,
        batches_failed: int,
        failed_section_labels: list[str],
    ) -> GraphExtractionResponse:
        """Merge section graphs by normalized name with stable first-seen IDs."""

        canonical_nodes: dict[str, ConceptNode] = {}
        endpoint_maps: list[dict[str, str]] = []

        for extraction in extractions:
            endpoint_map: dict[str, str] = {}
            for node in extraction.nodes:
                normalized_name = normalize_concept_name(node.name)
                if not normalized_name:
                    continue
                existing = canonical_nodes.get(normalized_name)
                if existing is None:
                    canonical_nodes[normalized_name] = node.model_copy(
                        update={"normalized_name": normalized_name}
                    )
                    endpoint_map[node.id] = node.id
                    continue

                endpoint_map[node.id] = existing.id
                canonical_nodes[normalized_name] = existing.model_copy(
                    update={
                        "source_chunk_ids": list(
                            dict.fromkeys(
                                [
                                    *existing.source_chunk_ids,
                                    *node.source_chunk_ids,
                                    node.source_chunk_id,
                                ]
                            )
                        ),
                        "page_numbers": list(
                            dict.fromkeys(
                                [
                                    *existing.page_numbers,
                                    *node.page_numbers,
                                    *([node.page_number] if node.page_number else []),
                                ]
                            )
                        ),
                        "section_headings": list(
                            dict.fromkeys(
                                heading
                                for heading in [
                                    *existing.section_headings,
                                    *node.section_headings,
                                    node.section_heading,
                                ]
                                if heading
                            )
                        ),
                    }
                )
            endpoint_maps.append(endpoint_map)

        relationships: list[ConceptRelationship] = []
        seen_relationships: set[tuple[str, str, str]] = set()
        for extraction, endpoint_map in zip(extractions, endpoint_maps, strict=True):
            for relationship in extraction.relationships:
                source_id = endpoint_map.get(relationship.source_node_id)
                target_id = endpoint_map.get(relationship.target_node_id)
                if not source_id or not target_id or source_id == target_id:
                    continue
                key = (source_id, target_id, relationship.relation_type)
                if key in seen_relationships:
                    continue
                seen_relationships.add(key)
                relationships.append(
                    relationship.model_copy(
                        update={
                            "source_node_id": source_id,
                            "target_node_id": target_id,
                        }
                    )
                )

        return GraphExtractionResponse(
            nodes=list(canonical_nodes.values()),
            relationships=relationships,
            sections_total=sections_total,
            sections_succeeded=sections_succeeded,
            sections_failed=sections_failed,
            batches_total=batches_total,
            batches_succeeded=batches_succeeded,
            batches_failed=batches_failed,
            failed_section_labels=failed_section_labels,
        )

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
