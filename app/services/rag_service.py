import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from groq import Groq
from neo4j import AsyncDriver
from pydantic import BaseModel, ConfigDict, Field
from qdrant_client import QdrantClient
from qdrant_client.http.exceptions import UnexpectedResponse
from qdrant_client.models import Document, FieldCondition, Filter, MatchAny

from app.core.config import settings
from app.core.database import neo4j_driver, qdrant_client
from app.services.course_service import ReadyCourseContext

logger = logging.getLogger(__name__)


class CypherGenerationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cypher: str = Field(..., min_length=1)
    parameters: dict[str, str | int | float | bool | list[str]] = Field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class GraphRetrievalResult:
    concepts: list[dict[str, Any]]
    prerequisite_names: list[str]
    cypher: str
    metadata: dict[str, Any]


class RetrievalService:
    def __init__(
        self,
        graph_driver: AsyncDriver = neo4j_driver,
        vector_client: QdrantClient = qdrant_client,
    ) -> None:
        self.graph_driver = graph_driver
        self.vector_client = vector_client
        self.collection_name = settings.qdrant_collection_name
        self._embedding_model = None

    async def retrieve(
        self,
        question: str,
        context: ReadyCourseContext,
        top_k: int = 10,
    ) -> dict[str, Any]:
        graph_result = await self.execute_graph_retrieval(
            question=question,
            context=context,
        )
        chunks = await asyncio.to_thread(
            self.search_qdrant,
            question,
            graph_result.prerequisite_names,
            context.document_ids,
            top_k,
        )
        return {
            "graph_context": graph_result.concepts,
            "graph_cypher": graph_result.cypher,
            "chunks": chunks,
            "graph_metadata": graph_result.metadata,
        }

    async def execute_graph_retrieval(
        self,
        question: str,
        context: ReadyCourseContext,
    ) -> GraphRetrievalResult:
        generated = self._fallback_cypher(question)
        cypher = self._validate_read_only_cypher(generated.cypher)
        parameters = {
            **generated.parameters,
            "question": question,
            "course_ids": context.graph_course_ids,
            "document_ids": context.document_ids,
        }

        async with self.graph_driver.session() as session:
            result = await session.run(cypher, parameters)
            # `Result.data()` converts relationships into lossy tuples. Keep
            # native Record values so relationship endpoints and properties survive.
            records = [record async for record in result]

            if not records:
                records = await self._fetch_course_graph(
                    session, context.graph_course_ids, context.document_ids
                )

            totals = await self._fetch_graph_totals(
                session, context.graph_course_ids, context.document_ids
            )

        concepts: list[dict[str, Any]] = []
        prerequisite_names: list[str] = []
        displayed_edge_ids: set[tuple[str, str, str]] = set()
        for record in records:
            concept = self._node_to_dict(record.get("concept"))
            related_concepts = [
                self._node_to_dict(node)
                for node in record.get("related_concepts", [])
                if node is not None
            ]
            relationships = [
                self._relationship_to_dict(rel)
                for rel in record.get("relationships", [])
                if rel is not None
            ]
            concepts.append({
                "concept": concept,
                "related_concepts": related_concepts,
                "prerequisites": [
                    node
                    for node in related_concepts
                    if any(
                        relationship.get("type") == "PREREQUISITE_OF"
                        and relationship.get("source") == node.get("id")
                        and relationship.get("target") == concept.get("id")
                        for relationship in relationships
                    )
                ],
                "relationships": relationships,
            })
            names_by_id = {
                str(node.get("id")): str(node.get("name"))
                for node in related_concepts
                if node.get("id") and node.get("name")
            }
            for relationship in relationships:
                edge_id = (
                    str(relationship.get("source", "")),
                    str(relationship.get("target", "")),
                    str(relationship.get("type", "")),
                )
                displayed_edge_ids.add(edge_id)
                if (
                    relationship.get("type") == "PREREQUISITE_OF"
                    and relationship.get("target") == concept.get("id")
                ):
                    prerequisite_name = names_by_id.get(str(relationship.get("source", "")))
                    if prerequisite_name:
                        prerequisite_names.append(prerequisite_name)

        return GraphRetrievalResult(
            concepts=concepts,
            prerequisite_names=sorted(set(prerequisite_names)),
            cypher=cypher,
            metadata={
                **totals,
                "displayed_nodes": len({
                    node.get("id")
                    for item in concepts
                    for node in [item["concept"], *item["related_concepts"]]
                    if node.get("id")
                }),
                "displayed_edges": len(displayed_edge_ids),
                "filter_reason": "query_subgraph",
            },
        )

    async def generate_cypher(self, question: str) -> CypherGenerationResponse:
        provider = settings.llm_provider.lower()
        if provider == "gemini":
            return await asyncio.to_thread(
                self._generate_cypher_with_gemini,
                question,
            )
        if provider == "groq":
            return await asyncio.to_thread(
                self._generate_cypher_with_groq,
                question,
            )
        raise ValueError(f"Unsupported LLM_PROVIDER: {settings.llm_provider}")

    def search_qdrant(
        self,
        question: str,
        prerequisite_names: list[str],
        document_ids: list[str],
        top_k: int = 10,
    ) -> list[Any]:
        if not self._collection_exists():
            logger.info("Qdrant collection %s does not exist yet.", self.collection_name)
            return []

        expanded_query = self._build_expanded_query(question, prerequisite_names)
        if settings.embedding_provider == "qdrant_cloud":
            query_vector = Document(
                text=expanded_query,
                model=settings.embedding_model_name,
            )
        else:
            query_vector = self.embedding_model.encode(
                expanded_query,
                convert_to_numpy=True,
                normalize_embeddings=True,
                show_progress_bar=False,
            ).tolist()
        query_filter = Filter(
            must=[
                FieldCondition(
                    key="upload_id",
                    match=MatchAny(any=document_ids),
                )
            ]
        )

        try:
            results = self.vector_client.query_points(
                collection_name=self.collection_name,
                query=query_vector,
                query_filter=query_filter,
                limit=top_k,
                with_payload=True,
            ).points
        except UnexpectedResponse as exc:
            if self._is_qdrant_not_found_error(exc):
                logger.info("Qdrant collection %s does not exist yet.", self.collection_name)
                return []
            raise
        except AttributeError:
            try:
                results = self.vector_client.search(
                    collection_name=self.collection_name,
                    query_vector=query_vector,
                    query_filter=query_filter,
                    limit=top_k,
                    with_payload=True,
                )
            except UnexpectedResponse as exc:
                if self._is_qdrant_not_found_error(exc):
                    logger.info("Qdrant collection %s does not exist yet.", self.collection_name)
                    return []
                raise

        chunks: list[dict[str, Any]] = []
        for point in results:
            payload = point.payload or {}
            chunks.append(
                {
                    "id": str(point.id),
                    "score": float(point.score),
                    "text": str(payload.get("text", "")),
                    "metadata": {
                        key: value
                        for key, value in payload.items()
                        if key != "text"
                    },
                }
            )
        return chunks

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

    def _collection_exists(self) -> bool:
        try:
            return bool(
                self.vector_client.collection_exists(
                    collection_name=self.collection_name,
                )
            )
        except UnexpectedResponse as exc:
            if self._is_qdrant_not_found_error(exc):
                return False
            raise
        except AttributeError:
            try:
                self.vector_client.get_collection(collection_name=self.collection_name)
            except Exception as exc:
                if "not found" in str(exc).lower() or "404" in str(exc):
                    return False
                raise
            return True

    @staticmethod
    def _is_qdrant_not_found_error(exc: UnexpectedResponse) -> bool:
        return exc.status_code == 404 and b"Collection" in exc.content

    def _generate_cypher_with_groq(
        self,
        question: str,
    ) -> CypherGenerationResponse:
        if not settings.groq_api_key:
            return self._fallback_cypher(question)

        client = Groq(
            api_key=settings.groq_api_key,
            timeout=settings.provider_timeout_seconds,
        )
        completion = client.chat.completions.create(
            model=settings.groq_model,
            messages=[
                {"role": "system", "content": self._cypher_system_prompt()},
                {"role": "user", "content": question},
            ],
            temperature=0,
            response_format={"type": "json_object"},
        )
        content = completion.choices[0].message.content or "{}"
        return CypherGenerationResponse.model_validate_json(content)

    def _generate_cypher_with_gemini(
        self,
        question: str,
    ) -> CypherGenerationResponse:
        if not settings.gemini_api_key:
            return self._fallback_cypher(question)

        import google.generativeai as genai

        genai.configure(api_key=settings.gemini_api_key)
        model = genai.GenerativeModel(settings.gemini_model)
        response = model.generate_content(
            [self._cypher_system_prompt(), question],
            generation_config={
                "temperature": 0,
                "response_mime_type": "application/json",
            },
            request_options={"timeout": settings.provider_timeout_seconds},
        )
        return CypherGenerationResponse.model_validate_json(response.text or "{}")

    @staticmethod
    def _cypher_system_prompt() -> str:
        schema = json.dumps(CypherGenerationResponse.model_json_schema(), indent=2)
        return (
            "Generate a single read-only Neo4j Cypher query for an academic concept graph. "
            "The graph uses (:Course {id}) nodes connected to "
            "(:Concept {id, name, type, description}) nodes by [:CONTAINS]. "
            "Always scope the query to MATCH (course:Course {id: $course_id}) and only "
            "return only concepts contained by that course. "
            "The query must return a variable named concept and a variable named related_concepts. "
            "related_concepts must contain adjacent Concept nodes with relationship direction preserved. "
            "Use parameters instead of interpolating user text. Do not write, merge, delete, "
            "create, set, call procedures, or use APOC. Return only JSON matching this schema:\n\n"
            f"{schema}"
        )

    @staticmethod
    def _fallback_cypher(question: str) -> CypherGenerationResponse:
        terms = [
            term.lower()
            for term in re.findall(r"[A-Za-z][A-Za-z0-9_+-]{2,}", question)
            if len(term) > 2
        ][:12]
        return CypherGenerationResponse(
            cypher="""
            MATCH (course:Course)-[:CONTAINS]->(concept:Concept)
            WHERE course.id IN $course_ids
              AND concept.upload_id IN $document_ids
              AND any(term IN $terms WHERE toLower(concept.name) CONTAINS term)
            OPTIONAL MATCH (concept)-[relationship]-(related:Concept)
            WHERE related IS NULL OR ((course)-[:CONTAINS]->(related) AND related.upload_id IN $document_ids)
            RETURN concept, collect(DISTINCT related) AS related_concepts,
                   collect(DISTINCT relationship) AS relationships
            LIMIT 5
            """,
            parameters={"terms": terms or [question.lower()]},
        )

    @staticmethod
    def _validate_read_only_cypher(cypher: str) -> str:
        stripped = cypher.strip()
        forbidden = re.compile(
            r"\b(CREATE|MERGE|DELETE|DETACH|SET|REMOVE|DROP|CALL|LOAD|FOREACH)\b",
            re.IGNORECASE,
        )
        if forbidden.search(stripped):
            raise ValueError("Generated Cypher contains a forbidden write operation.")
        if "concept" not in stripped or "related_concepts" not in stripped:
            raise ValueError("Generated Cypher must return concept and related_concepts.")
        return stripped

    @staticmethod
    def _node_to_dict(node: Any) -> dict[str, Any]:
        if node is None:
            return {}
        return dict(node)

    @staticmethod
    def _relationship_to_dict(relationship: Any) -> dict[str, Any]:
        properties = dict(relationship.items())
        return {
            **properties,
            "type": relationship.type,
            "source": relationship.start_node.get("id"),
            "target": relationship.end_node.get("id"),
            "direction": "outgoing",
        }

    @staticmethod
    def _build_expanded_query(question: str, prerequisite_names: list[str]) -> str:
        if not prerequisite_names:
            return question
        graph_terms = " ".join(prerequisite_names)
        return f"{question}\nRelevant prerequisite concepts: {graph_terms}"

    async def _fetch_course_graph(
        self,
        session,
        course_ids: list[str],
        document_ids: list[str],
    ) -> list[dict[str, Any]]:
        result = await session.run(
            """
            MATCH (course:Course)-[:CONTAINS]->(concept:Concept)
            WHERE course.id IN $course_ids
              AND concept.upload_id IN $document_ids
            OPTIONAL MATCH (concept)-[relationship]-(related:Concept)
            WHERE related IS NULL OR ((course)-[:CONTAINS]->(related) AND related.upload_id IN $document_ids)
            RETURN concept, collect(DISTINCT related) AS related_concepts,
                   collect(DISTINCT relationship) AS relationships
            ORDER BY concept.name
            LIMIT 50
            """,
            course_ids=course_ids,
            document_ids=document_ids,
        )
        return [record async for record in result]

    async def _fetch_graph_totals(
        self, session, course_ids: list[str], document_ids: list[str]
    ) -> dict[str, int]:
        result = await session.run(
            """
            MATCH (course:Course)-[:CONTAINS]->(concept:Concept)
            WHERE course.id IN $course_ids
              AND concept.upload_id IN $document_ids
            OPTIONAL MATCH (concept)-[relationship]->(target:Concept)
            WHERE target.upload_id IN $document_ids
              AND (course)-[:CONTAINS]->(target)
            RETURN count(DISTINCT concept) AS total_nodes,
                   count(DISTINCT relationship) AS total_edges
            """,
            course_ids=course_ids,
            document_ids=document_ids,
        )
        record = await result.single()
        return {
            "total_nodes": int(record["total_nodes"] if record else 0),
            "total_edges": int(record["total_edges"] if record else 0),
        }

    @staticmethod
    def _resolve_embedding_device() -> str:
        import torch

        if torch.backends.mps.is_available():
            return "mps"
        return "cpu"
