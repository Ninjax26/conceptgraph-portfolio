import asyncio
import hashlib
import json
import logging
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace

from groq import (
    APIConnectionError,
    APITimeoutError,
    AuthenticationError,
    PermissionDeniedError,
    BadRequestError,
    Groq,
    InternalServerError,
    RateLimitError,
)
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
from app.core.processing import minimum_graph_relationships
from app.core.exceptions import (
    GraphFailureReason,
    GraphStructureError,
    LLMConfigurationError,
    LLMProviderRateLimitError,
    LLMProviderRequestError,
    LLMProviderUnavailableError,
    LLMProviderTruncatedResponseError,
)
from app.schemas.extraction import (
    ALLOWED_RELATIONSHIP_TYPES,
    ConceptNode,
    ConceptRelationship,
    GraphBatchDiagnostic,
    GraphExtractionResponse,
    normalize_concept_name,
)
from app.services.cerebras_service import cerebras_service
from app.services.gemini_service import gemini_service
from app.services.parser_service import DocumentChunk
from app.services.provider_failover import provider_circuit_breaker
from app.services.provider_metrics import record_provider_usage
from app.services.shared_provider_service import observe_provider_call
from app.services.retrieval_candidates import identifier_terms
from app.services.graph_retry_policy import quota_retry_delay


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class GraphSectionBatch:
    batch_key: str
    section_key: str
    section_label: str
    chunks: tuple[DocumentChunk, ...]
    kind: str = "section"


@dataclass(frozen=True, slots=True)
class GraphProjectionMarker:
    """Identity of one intended projection, persisted with its Neo4j write."""

    id: str
    job_id: str
    upload_id: str
    course_id: str
    document_task_id: str
    payload_sha256: str

    def as_parameters(self) -> dict[str, str]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


class IngestionService:
    MAX_CONCEPTS_PER_BATCH = 8
    MAX_RELATIONSHIPS_PER_BATCH = 10
    MAX_GRAPH_OUTPUT_TOKENS = 2_000
    GRAPH_CONTEXT_LIMITS = (5000, 3000)
    # Bump when validation/provenance changes are not captured by prompt/schema
    # hashes. Secrets and operating quotas never belong in checkpoint identities.
    GRAPH_CHECKPOINT_CONTRACT_VERSION = 1

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
                    "identifier_terms": identifier_terms(chunk.text),
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
        *,
        completed_batches: Mapping[str, GraphExtractionResponse] | None = None,
        on_batch_completed: Callable[
            [GraphSectionBatch, GraphExtractionResponse], Awaitable[None]
        ]
        | None = None,
    ) -> GraphExtractionResponse:
        if not chunks:
            return GraphExtractionResponse()

        completed_batches = completed_batches or {}
        candidate_batches = self._graph_section_batches(
            chunks,
            batch_size=settings.graph_batch_size,
        )
        cached_by_key = {
            batch.batch_key: checked
            for batch in candidate_batches
            if (checked := self._validated_checkpoint(
                completed_batches.get(batch.batch_key), batch
            )) is not None
        }
        missing_batches = [
            batch for batch in candidate_batches if batch.batch_key not in cached_by_key
        ]
        batches = self._select_graph_batches(
            missing_batches,
            settings.graph_max_batches,
        )
        section_labels = {
            batch.section_key: batch.section_label
            for batch in candidate_batches
        }
        represented_sections: set[str] = set()
        successful_batches = len(cached_by_key)
        failed_batches = 0
        skipped_batches = len(missing_batches) - len(batches)
        budget_applied = skipped_batches > 0
        provider_limited = False
        provider_retry_after = None
        consecutive_timeouts = 0
        partials: list[GraphExtractionResponse] = []
        completed_section_batches: list[GraphSectionBatch] = []
        completed_keys = set(cached_by_key)
        diagnostics: list[GraphBatchDiagnostic] = []
        empty_batches = sum(not extraction.nodes for extraction in cached_by_key.values())

        def diagnostic(batch: GraphSectionBatch, outcome: str, category: str) -> None:
            diagnostics.append(GraphBatchDiagnostic(
                section_label=batch.section_label, kind=batch.kind,
                outcome=outcome, category=category,
            ))

        selected_keys = {batch.batch_key for batch in batches}
        for batch in missing_batches:
            if batch.batch_key not in selected_keys:
                diagnostic(batch, "skipped", "budget")

        for batch in candidate_batches:
            extraction = cached_by_key.get(batch.batch_key)
            if extraction is None:
                continue
            partials.append(extraction)
            diagnostics.extend(extraction.batch_diagnostics)
            if extraction.nodes:
                completed_section_batches.append(batch)
                represented_sections.add(batch.section_key)
            else:
                diagnostic(batch, "empty", "no_concepts")

        for batch_index, batch in enumerate(batches):
            context = "\n\n".join(
                self._format_graph_excerpt(chunk)
                for chunk in batch.chunks
            )
            try:
                extraction = await self.extract_graph_from_text(context)
                enriched = self._attach_provenance(extraction, batch.chunks)
                if extraction.nodes and not enriched.nodes:
                    raise GraphStructureError("No extracted concepts cite the supplied source.", reason=GraphFailureReason.UNKNOWN_SOURCE)
            except (RateLimitError, LLMProviderRateLimitError) as exc:
                failed_batches += 1
                provider_limited = True
                provider_retry_after = quota_retry_delay(exc)
                category = self._graph_failure_category(exc)
                diagnostic(batch, "failed", category)
                skipped_batches += len(batches) - batch_index - 1
                for pending in batches[batch_index + 1:]:
                    diagnostic(pending, "skipped", category)
                logger.warning(
                    "Graph extraction stopped because the provider quota was reached; "
                    "preserving completed graph batches"
                )
                break
            except (
                APIConnectionError,
                APITimeoutError,
                InternalServerError,
                LLMProviderUnavailableError,
            ) as exc:
                failed_batches += 1
                consecutive_timeouts += 1
                diagnostic(batch, "failed", self._graph_failure_category(exc))
                logger.warning(
                    "Skipped graph batch: category=provider_unavailable",
                )
                if consecutive_timeouts >= 2:
                    skipped_batches += len(batches) - batch_index - 1
                    for pending in batches[batch_index + 1:]:
                        diagnostic(pending, "skipped", "provider_unavailable")
                    logger.warning(
                        "Graph extraction stopped after repeated provider timeouts; "
                        "preserving completed graph batches"
                    )
                    break
                continue
            except GraphStructureError as exc:
                failed_batches += 1
                diagnostic(batch, "failed", exc.reason.value)
                logger.warning(
                    "Skipped invalid graph batch: category=%s", exc.reason.value,
                )
                continue
            except (LLMConfigurationError, AuthenticationError, PermissionDeniedError,
                    BadRequestError, LLMProviderRequestError) as exc:
                failed_batches += 1
                category = self._graph_failure_category(exc)
                diagnostic(batch, "failed", category)
                skipped_batches += len(batches) - batch_index - 1
                for pending in batches[batch_index + 1:]:
                    diagnostic(pending, "skipped", category)
                logger.warning("Graph extraction stopped: category=%s", category)
                break

            consecutive_timeouts = 0
            successful_batches += 1
            completed_keys.add(batch.batch_key)
            if enriched.nodes:
                completed_section_batches.append(batch)
                represented_sections.add(batch.section_key)
                if len(enriched.nodes) < len(extraction.nodes):
                    filtered = GraphBatchDiagnostic(
                        section_label=batch.section_label, outcome="filtered", category="unknown_source",
                        discarded_nodes=len(extraction.nodes) - len(enriched.nodes),
                        discarded_relationships=len(extraction.relationships) - len(enriched.relationships),
                    )
                    diagnostics.append(filtered)
                    enriched = enriched.model_copy(update={"batch_diagnostics": [filtered]})
            else:
                empty_batches += 1
                diagnostic(batch, "empty", "no_concepts")
            partials.append(enriched)
            if on_batch_completed is not None:
                await on_batch_completed(batch, enriched)

        processed_sections = {
            key for key in section_labels
            if all(batch.batch_key in completed_keys for batch in candidate_batches if batch.section_key == key)
        }
        empty_sections = processed_sections - represented_sections
        failed_sections = set(section_labels) - processed_sections
        merged = self._merge_graph_extractions(
            partials,
            sections_total=len(section_labels),
            sections_succeeded=len(represented_sections),
            sections_failed=len(failed_sections),
            batches_total=len(candidate_batches),
            batches_succeeded=successful_batches,
            batches_failed=failed_batches,
            batches_skipped=skipped_batches,
            provider_limited=provider_limited,
            extraction_budget_applied=budget_applied,
            failed_section_labels=sorted(
                section_labels[key]
                for key in failed_sections
            ),
        )

        global_attempted = False
        global_succeeded = False
        global_processed = False
        if (
            settings.graph_global_linking_enabled
            and not provider_limited
            and len(merged.nodes) >= 2
            and (
                len(represented_sections) >= 2
                or len(merged.relationships) < minimum_graph_relationships(len(merged.nodes))
            )
        ):
            recovery = self._relationship_recovery_request(completed_section_batches, merged.nodes)
            if recovery is not None:
                global_batch, context = recovery
                global_attempted = True
                linked = self._validated_checkpoint(
                    completed_batches.get(global_batch.batch_key), global_batch
                )
                if linked is None:
                    try:
                        extraction = await self.extract_graph_from_text(context)
                        linked = self._attach_provenance(
                            extraction, global_batch.chunks
                        )
                        if extraction.nodes and not linked.nodes:
                            raise GraphStructureError("No linked concepts cite the supplied source.", reason=GraphFailureReason.UNKNOWN_SOURCE)
                        if on_batch_completed is not None:
                            await on_batch_completed(global_batch, linked)
                    except (
                        RateLimitError,
                        LLMProviderRateLimitError,
                        APIConnectionError,
                        APITimeoutError,
                        InternalServerError,
                        LLMProviderUnavailableError,
                        LLMProviderRequestError,
                        LLMConfigurationError,
                        AuthenticationError,
                        PermissionDeniedError,
                        BadRequestError,
                        GraphStructureError,
                    ) as exc:
                        if isinstance(exc, (RateLimitError, LLMProviderRateLimitError)):
                            provider_retry_after = quota_retry_delay(exc)
                            merged = merged.model_copy(update={"provider_limited": True})
                        diagnostic(global_batch, "failed", self._graph_failure_category(exc))
                        logger.warning(
                            "Optional graph relationship pass was skipped; "
                            "the validated section graph remains usable"
                        )
                        linked = None
                global_processed = linked is not None
                if linked is not None and linked.nodes:
                    previous_relationship_count = len(merged.relationships)
                    merged = self._merge_graph_extractions(
                        [merged, linked],
                        sections_total=merged.sections_total,
                        sections_succeeded=merged.sections_succeeded,
                        sections_failed=merged.sections_failed,
                        batches_total=merged.batches_total,
                        batches_succeeded=merged.batches_succeeded,
                        batches_failed=merged.batches_failed,
                        batches_skipped=merged.batches_skipped,
                        provider_limited=merged.provider_limited,
                        extraction_budget_applied=merged.extraction_budget_applied,
                        failed_section_labels=merged.failed_section_labels,
                    )
                    global_succeeded = len(merged.relationships) > previous_relationship_count
                    if not global_succeeded:
                        diagnostic(global_batch, "empty", "no_new_relationships")
                elif linked is not None:
                    diagnostic(global_batch, "empty", "no_concepts")

        return merged.model_copy(
            update={
                "global_linking_attempted": global_attempted,
                "global_linking_succeeded": global_succeeded,
                "global_linking_processed": global_processed,
                "provider_retry_after_seconds": provider_retry_after,
                "sections_processed": len(processed_sections),
                "sections_empty": len(empty_sections),
                "batches_empty": empty_batches,
                "batch_diagnostics": diagnostics,
            }
        )

    @staticmethod
    def _graph_failure_category(exc: Exception) -> str:
        if getattr(exc, "application_budget", False):
            return "application_budget"
        if isinstance(exc, GraphStructureError):
            return exc.reason.value
        if isinstance(exc, (RateLimitError, LLMProviderRateLimitError)):
            return "quota"
        if isinstance(exc, (LLMConfigurationError, AuthenticationError, PermissionDeniedError)):
            return "configuration"
        if isinstance(exc, (BadRequestError, LLMProviderRequestError)):
            return "provider_request"
        return "provider_unavailable"

    async def extract_graph_from_text(self, text: str) -> GraphExtractionResponse:
        provider = settings.llm_provider.lower()
        if provider == "groq":
            return await self._extract_with_failover(text)
        if provider not in {"gemini", "cerebras"}:
            raise ValueError(f"Unsupported LLM_PROVIDER: {settings.llm_provider}")
        try:
            return await self._graph_provider_attempt(provider, text)
        except GraphStructureError:
            if not settings.graph_json_repair_enabled:
                raise
            return await self._graph_provider_attempt(provider, text, repair=True)

    async def _graph_provider_attempt(
        self, provider: str, text: str, *, failover: bool = False, repair: bool = False,
    ) -> GraphExtractionResponse:
        request_text = self._graph_extraction_contexts(text)[-1 if repair else 0]
        extractors = {
            "groq": self._extract_with_groq,
            "gemini": self._extract_with_gemini,
            "cerebras": self._extract_with_cerebras,
        }
        extractor = extractors[provider]

        def generate() -> GraphExtractionResponse:
            # Orchestration owns the single repair allowance across all providers.
            kwargs = {"repair": repair}
            if provider != "gemini":
                kwargs["allow_repair"] = False
            return self._validate_provider_graph(extractor(request_text, **kwargs), text)

        result = await observe_provider_call(
            provider, "graph_extraction", generate,
            input_characters=len(request_text), failover=failover, repair=repair,
        )
        provider_circuit_breaker.record_success(provider)
        return result

    async def _extract_with_failover(self, text: str) -> GraphExtractionResponse:
        providers = self._graph_provider_route()
        if not providers:
            raise LLMConfigurationError(
                "GROQ_API_KEY, CEREBRAS_API_KEY, or GEMINI_API_KEY is required when LLM_PROVIDER=groq"
            )

        last_error: Exception | None = None
        repair_provider: str | None = None
        for provider in providers:
            if not provider_circuit_breaker.is_available(provider):
                continue
            try:
                return await self._graph_provider_attempt(
                    provider, text, failover=provider != "groq",
                )
            except GraphStructureError as exc:
                last_error = exc
                repair_provider = provider
                logger.warning("Graph provider returned invalid output: provider=%s category=%s", provider, exc.reason.value)
            except (
                RateLimitError, LLMProviderRateLimitError, AuthenticationError,
                PermissionDeniedError, APIConnectionError, APITimeoutError,
                InternalServerError, LLMProviderUnavailableError, LLMConfigurationError,
            ) as exc:
                last_error = exc
                repair_provider = None
                provider_circuit_breaker.block(
                    provider, settings.llm_failover_cooldown_seconds, exc,
                )
                logger.warning("Graph provider unavailable: provider=%s category=%s", provider, self._graph_failure_category(exc))
            except (BadRequestError, LLMProviderRequestError) as exc:
                last_error = exc
                repair_provider = None
                logger.warning("Graph provider rejected request: provider=%s", provider)

        # At most one extra request per batch, after normal provider failover.
        # Quota, timeout, authentication, and request errors never trigger repair.
        if repair_provider is not None and settings.graph_json_repair_enabled:
            try:
                return await self._graph_provider_attempt(
                    repair_provider, text, failover=repair_provider != "groq", repair=True,
                )
            except (GraphStructureError, BadRequestError, LLMProviderRequestError) as exc:
                last_error = exc
            except (RateLimitError, LLMProviderRateLimitError, AuthenticationError,
                    PermissionDeniedError, APIConnectionError, APITimeoutError,
                    InternalServerError, LLMProviderUnavailableError, LLMConfigurationError) as exc:
                last_error = exc
                provider_circuit_breaker.block(repair_provider, settings.llm_failover_cooldown_seconds, exc)

        if isinstance(last_error, (RateLimitError, LLMProviderRateLimitError)):
            if getattr(last_error, "application_budget", False):
                raise last_error
            raise LLMProviderRateLimitError(
                "Configured graph providers have exhausted their available quota",
                retry_after_seconds=getattr(last_error, "retry_after_seconds", None),
            ) from last_error
        if isinstance(last_error, (APIConnectionError, APITimeoutError, InternalServerError)):
            raise LLMProviderUnavailableError(
                "Configured graph providers are temporarily unavailable"
            ) from last_error
        if last_error is not None:
            raise last_error
        raise LLMProviderUnavailableError("No graph-generation provider is available")

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
            await self._write_graph_extraction(
                session, extraction, course_id, upload_id=upload_id,
                document_name=document_name, course_name=course_name,
                execution_token=execution_token,
            )

    async def _write_graph_extraction(
        self, session, extraction: GraphExtractionResponse, course_id: str, *,
        upload_id: str, document_name: str, course_name: str, execution_token: str,
    ) -> None:
        # Shared query shape for the existing auto-commit ingestion path and the
        # future continuation path's managed transaction. Do not change scope.
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

    async def replace_graph_extraction(
        self, extraction: GraphExtractionResponse, course_id: str, *,
        upload_id: str, document_name: str, course_name: str,
        execution_token: str,
        marker: GraphProjectionMarker | None = None,
    ) -> None:
        """Replace one upload projection atomically; caller owns its PG fence.

        Not wired to automatic continuation yet. The original READY execution
        token must be retained so vectors and concepts remain in the same scope.
        """
        from app.core.blocking import run_async_safely

        if not all(isinstance(value, str) and value.strip() for value in (
            course_id, upload_id, document_name, execution_token,
        )):
            raise ValueError("Graph replacement requires a complete document identity.")
        # This is the merged upload graph, not one provider batch: applying the
        # 8-node/10-edge batch limits here would reject valid larger documents.
        payload = extraction.model_dump(mode="json")
        if any(row["relation_type"] not in ALLOWED_RELATIONSHIP_TYPES for row in payload["relationships"]):
            raise ValueError("Graph replacement contains an unsupported relationship.")
        checked = GraphExtractionResponse.model_validate(payload)
        if len(checked.nodes) != len(extraction.nodes) or len(checked.relationships) != len(extraction.relationships):
            raise ValueError("Graph replacement must already be normalized and validated.")
        if any(node.upload_id != upload_id or node.document_name != document_name
               or not node.source_chunk_id or node.page_number is None for node in checked.nodes):
            raise ValueError("Graph replacement requires bound source provenance.")
        if marker is not None and (
            not all(isinstance(value, str) and value.strip() for value in marker.as_parameters().values())
            or (marker.upload_id, marker.course_id, marker.document_task_id)
               != (upload_id, course_id, execution_token)
            or re.fullmatch(r"[0-9a-f]{64}", marker.payload_sha256) is None
        ):
            raise ValueError("Graph publication marker does not match the replacement identity.")

        async def replace(tx):
            result = await tx.run(
                """
                MATCH (concept:Concept {course_id: $course_id, upload_id: $upload_id})
                DETACH DELETE concept
                """, course_id=course_id, upload_id=upload_id,
            )
            await result.consume()
            await self._write_graph_extraction(
                tx, checked, course_id, upload_id=upload_id,
                document_name=document_name, course_name=course_name,
                execution_token=execution_token,
            )
            # Empty replacement must not leave a phantom course; other uploads
            # in the same course retain their nodes, edges and CONTAINS links.
            result = await tx.run(
                """
                MATCH (course:Course {id: $course_id})
                WHERE NOT (course)-[:CONTAINS]->(:Concept)
                DETACH DELETE course
                """, course_id=course_id,
            )
            await result.consume()
            if marker is not None:
                result = await tx.run(
                    """
                    MERGE (marker:GraphPublication {id: $id})
                    ON CREATE SET marker.job_id = $job_id,
                                  marker.upload_id = $upload_id,
                                  marker.course_id = $course_id,
                                  marker.document_task_id = $document_task_id,
                                  marker.payload_sha256 = $payload_sha256
                    WITH marker
                    WHERE marker.job_id = $job_id AND marker.upload_id = $upload_id
                      AND marker.course_id = $course_id
                      AND marker.document_task_id = $document_task_id
                      AND marker.payload_sha256 = $payload_sha256
                    RETURN marker.id AS id
                    """, **marker.as_parameters(),
                )
                if await result.single() is None:
                    raise ValueError("Graph publication marker conflicts with an existing write.")

        async def publish():
            async with self.graph_driver.session() as session:
                # Managed retries repeat the entire idempotent replacement;
                # never retry deletion and insertion as separate transactions.
                await session.execute_write(replace)

        await run_async_safely(publish())

    async def read_graph_publication_marker(self, intent_id: str) -> GraphProjectionMarker | None:
        """Read remote commit evidence; caller must reconcile it with PostgreSQL."""
        if not isinstance(intent_id, str) or not intent_id.strip():
            raise ValueError("Graph publication intent ID is required.")
        async with self.graph_driver.session() as session:
            result = await session.run(
                """MATCH (marker:GraphPublication {id: $id})
                RETURN marker.id AS id, marker.job_id AS job_id,
                       marker.upload_id AS upload_id, marker.course_id AS course_id,
                       marker.document_task_id AS document_task_id,
                       marker.payload_sha256 AS payload_sha256""", id=intent_id,
            )
            row = await result.single()
        return GraphProjectionMarker(**dict(row)) if row is not None else None

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
        from app.core.blocking import run_blocking_safely
        await run_blocking_safely(self.cleanup_vectors, upload_id)
        await self.cleanup_graph(upload_id, course_id)

    def cleanup_vectors(self, upload_id: str) -> None:
        """Remove only vector points for an upload."""

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

    async def cleanup_graph(self, upload_id: str, course_id: str) -> None:
        """Remove only the Neo4j projection for an upload, preserving vectors."""

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
                MATCH (marker:GraphPublication {course_id: $course_id, upload_id: $upload_id})
                DETACH DELETE marker
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
        for field in ("upload_id", "identifier_terms"):
            if field in payload_schema:
                continue
            try:
                self.vector_client.create_payload_index(
                    collection_name=self.collection_name, field_name=field,
                    field_schema=PayloadSchemaType.KEYWORD, wait=True,
                )
            except UnexpectedResponse as exc:
                if exc.status_code != 409 and b"already exists" not in exc.content.lower():
                    raise

    def _extract_with_groq(
        self, text: str, *, repair: bool = False, allow_repair: bool | None = None,
    ) -> GraphExtractionResponse:
        if not settings.groq_api_key:
            raise LLMConfigurationError("GROQ_API_KEY is required when LLM_PROVIDER=groq")

        contexts = self._graph_extraction_contexts(text)
        client = Groq(
            api_key=settings.groq_api_key,
            timeout=settings.provider_timeout_seconds,
            max_retries=0,
        )
        try:
            completion = client.chat.completions.create(
                model=settings.groq_model,
                messages=[
                    {
                        "role": "system",
                        "content": self._extraction_system_prompt() + (
                            " Return one JSON object with exactly two arrays named nodes and relationships."
                            if repair else ""
                        ),
                    },
                    {
                        "role": "user",
                        "content": contexts[-1] if repair else contexts[0],
                    },
                ],
                temperature=0,
                max_completion_tokens=self.MAX_GRAPH_OUTPUT_TOKENS,
                response_format={"type": "json_object"} if repair else {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "concept_graph_extraction",
                        "strict": True,
                        "schema": self._graph_extraction_schema(),
                    },
                },
            )
            usage = getattr(completion, "usage", None)
            if usage is not None:
                record_provider_usage(getattr(usage, "prompt_tokens", None), getattr(usage, "completion_tokens", None),
                                      getattr(usage, "total_tokens", None))
            choice = completion.choices[0]
            if getattr(choice, "finish_reason", None) == "length":
                raise GraphStructureError("The graph response was truncated.", reason=GraphFailureReason.TRUNCATED_RESPONSE)
            return self._validate_provider_graph(self._parse_provider_graph(choice.message.content or "{}"), text)
        except BadRequestError as exc:
            if not self._is_json_validation_failure(exc):
                raise
            error = GraphStructureError("The provider rejected the graph schema.")
        except GraphStructureError as exc:
            error = exc
        finally:
            client.close()
        repair_allowed = settings.graph_json_repair_enabled if allow_repair is None else allow_repair
        if repair or not repair_allowed:
            raise error
        return self._extract_with_groq(text, repair=True, allow_repair=False)

    def _extract_with_cerebras(
        self, text: str, *, repair: bool = False, allow_repair: bool | None = None,
    ) -> GraphExtractionResponse:
        contexts = self._graph_extraction_contexts(text)
        try:
            content = cerebras_service.complete(
                [
                    {"role": "system", "content": self._extraction_system_prompt() + (
                        " Return one JSON object with exactly two arrays named nodes and relationships."
                        if repair else ""
                    )},
                    {"role": "user", "content": contexts[-1] if repair else contexts[0]},
                ],
                max_tokens=self.MAX_GRAPH_OUTPUT_TOKENS,
                response_format={"type": "json_object"} if repair else {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "concept_graph_extraction",
                        "strict": True,
                        "schema": self._cerebras_graph_extraction_schema(),
                    },
                },
            )
            return self._validate_provider_graph(self._parse_provider_graph(content), text)
        except LLMProviderRequestError:
            # A rejected request is not evidence that retrying JSON will help.
            raise
        except GraphStructureError as exc:
            error = exc
        repair_allowed = settings.graph_json_repair_enabled if allow_repair is None else allow_repair
        if repair or not repair_allowed:
            raise error
        return self._extract_with_cerebras(text, repair=True, allow_repair=False)

    @staticmethod
    def _graph_extraction_contexts(text: str) -> tuple[str, ...]:
        """Bound whole source excerpts, preserving IDs and evidence across paragraphs."""
        contexts: list[str] = []
        parts = [part.strip() for part in re.split(r"(?m)(?=^\[Source chunk ID:)", text) if part.strip()]
        excerpts = [part for part in parts if part.startswith("[Source chunk ID:")]
        prologue = "\n\n".join(part for part in parts if not part.startswith("[Source chunk ID:"))
        for limit in IngestionService.GRAPH_CONTEXT_LIMITS:
            if not excerpts:
                shortened = text.strip()[:limit].strip()
            else:
                prefix = prologue[:1000].strip()
                separators = 2 * (len(excerpts) - 1 + bool(prefix))
                per_excerpt = (limit - len(prefix) - separators) // len(excerpts)
                bounded = []
                for excerpt in excerpts:
                    header, _, body = excerpt.partition("\n")
                    if len(header) + 80 > per_excerpt:
                        source_id = header.partition(":")[2].split("|", 1)[0].rstrip("]").strip()
                        header = f"[Source chunk ID: {source_id}]"
                    if len(header) > per_excerpt:
                        raise GraphStructureError("Source identifiers exceed the graph context budget.", reason=GraphFailureReason.BATCH_LIMIT)
                    bounded.append(IngestionService._truncate_excerpt(f"{header}\n{body}", per_excerpt))
                shortened = "\n\n".join(([prefix] if prefix else []) + bounded).strip()
            if shortened and (not contexts or shortened != contexts[-1]):
                contexts.append(shortened)
        return tuple(contexts or [""])

    @staticmethod
    def _cerebras_graph_extraction_schema() -> dict:
        schema = IngestionService._graph_extraction_schema()
        # Cerebras structured outputs enforce the shape, while array-size limits
        # remain in the prompt and local validation because maxItems is unsupported.
        schema["properties"]["nodes"].pop("maxItems", None)
        schema["properties"]["relationships"].pop("maxItems", None)
        return schema

    @staticmethod
    def _validate_graph_batch_size(
        extraction: GraphExtractionResponse,
    ) -> GraphExtractionResponse:
        if len(extraction.nodes) > IngestionService.MAX_CONCEPTS_PER_BATCH:
            raise GraphStructureError("Graph extraction exceeded the concept limit.", reason=GraphFailureReason.BATCH_LIMIT)
        if (
            len(extraction.relationships)
            > IngestionService.MAX_RELATIONSHIPS_PER_BATCH
        ):
            raise GraphStructureError("Graph extraction exceeded the relationship limit.", reason=GraphFailureReason.BATCH_LIMIT)
        return extraction

    @staticmethod
    def _validate_provider_graph(
        extraction: GraphExtractionResponse,
        source_text: str,
    ) -> GraphExtractionResponse:
        """Reject structurally valid output that cannot produce a sourced graph."""
        if not extraction.nodes:
            return extraction.model_copy(update={"empty_extraction": True})

        source_ids = {
            match.strip()
            for match in re.findall(
                r"\[Source chunk ID:\s*([^|\]\n]+)",
                source_text,
            )
            if match.strip()
        }
        if source_ids and not any(
            node.source_chunk_id in source_ids for node in extraction.nodes
        ):
            raise GraphStructureError(
                "The provider graph did not reference any supplied source chunk.",
                reason=GraphFailureReason.UNKNOWN_SOURCE,
            )
        return extraction

    @classmethod
    def _parse_provider_graph(cls, content: str) -> GraphExtractionResponse:
        try:
            payload = json.loads(content)
        except (ValueError, TypeError) as exc:
            raise GraphStructureError("The graph response is not valid JSON.", reason=GraphFailureReason.MALFORMED_JSON) from exc
        if not isinstance(payload, dict) or set(payload) != {"nodes", "relationships"}:
            raise GraphStructureError("The graph response must contain nodes and relationships arrays.")
        if not isinstance(payload["nodes"], list) or not isinstance(payload["relationships"], list):
            raise GraphStructureError("The graph response must contain nodes and relationships arrays.")
        # Enforce limits before normalization can hide an oversized response.
        if len(payload["nodes"]) > cls.MAX_CONCEPTS_PER_BATCH or len(payload["relationships"]) > cls.MAX_RELATIONSHIPS_PER_BATCH:
            raise GraphStructureError("Graph extraction exceeded the batch limit.", reason=GraphFailureReason.BATCH_LIMIT)
        for relationship in payload["relationships"]:
            if isinstance(relationship, dict) and isinstance(relationship.get("relation_type"), str):
                relation_type = re.sub(r"[^A-Z0-9]+", "_", relationship["relation_type"].strip().upper()).strip("_")
                if relation_type not in ALLOWED_RELATIONSHIP_TYPES:
                    raise GraphStructureError("The graph used an unsupported relationship type.", reason=GraphFailureReason.UNSUPPORTED_RELATIONSHIP)
        try:
            return GraphExtractionResponse.model_validate(payload)
        except ValidationError as exc:
            messages = [error["msg"] for error in exc.errors(include_input=False, include_url=False)]
            reason = GraphFailureReason.INVALID_SCHEMA
            if any("not extracted" in message for message in messages):
                reason = GraphFailureReason.INVALID_ENDPOINTS
            elif any("duplicate concept IDs" in message for message in messages):
                reason = GraphFailureReason.DUPLICATE_IDS
            raise GraphStructureError("The graph response failed structural validation.", reason=reason) from exc

    @staticmethod
    def _is_json_validation_failure(exc: BadRequestError) -> bool:
        body = exc.body
        if isinstance(body, dict):
            error = body.get("error")
            if isinstance(error, dict) and error.get("code") == "json_validate_failed":
                return True
        return "json_validate_failed" in str(exc).lower()

    def _extract_with_gemini(self, text: str, *, repair: bool = False) -> GraphExtractionResponse:
        if not settings.gemini_api_key:
            raise LLMConfigurationError("GEMINI_API_KEY is required when LLM_PROVIDER=gemini")

        context = self._graph_extraction_contexts(text)[-1 if repair else 0]
        try:
            content = gemini_service.generate(
                [self._extraction_system_prompt() + (
                    " Regenerate a minimal graph with exact source IDs and every endpoint in nodes."
                    if repair else ""
                ), context],
                json_mode=True,
                response_schema=None if repair else self._graph_extraction_schema(),
                max_output_tokens=self.MAX_GRAPH_OUTPUT_TOKENS,
                require_complete=True,
            )
        except LLMProviderTruncatedResponseError as exc:
            raise GraphStructureError("The graph response was truncated.", reason=GraphFailureReason.TRUNCATED_RESPONSE) from exc
        return self._validate_provider_graph(self._parse_provider_graph(content), text)

    @staticmethod
    def _extraction_system_prompt() -> str:
        relationship_types = ", ".join(sorted(ALLOWED_RELATIONSHIP_TYPES))
        return (
            "Extract academic concepts and relationships from the supplied source excerpts. "
            "Return only the requested structured response. Use stable lowercase snake_case ids for nodes. "
            "Keep concept descriptions to one short sentence. If the excerpts support no "
            "academic concepts, return explicit empty nodes and relationships arrays. "
            "Every concept must cite exactly one supplied Source chunk ID in "
            "source_chunk_id; never invent a source ID. Every relationship endpoint must "
            "reference an extracted node. The only permitted relationship types are: "
            f"{relationship_types}. Use these directions exactly: A PREREQUISITE_OF B means A "
            "must be understood before B; A PART_OF B means A is a component or subtopic of B; "
            "A EXPLAINS B means A directly clarifies B; A CAUSES B means A produces B; and "
            "A APPLIES_TO B means A is used in B. RELATED_TO is symmetric and should be used "
            "only when no more specific relation is supported. Prefer a small connected graph "
            "over a longer list of isolated terms. When two extracted concepts have a relation "
            "explicitly supported by the excerpts, include that edge, but never invent an edge "
            "from document order or general knowledge. Use the expanded concept name consistently "
            "when the excerpts explicitly define its abbreviation; do not create separate nodes "
            "for that abbreviation and its expanded name. Do not use PART_OF merely because "
            "a person or role is responsible for an activity. Extract at most "
            f"{IngestionService.MAX_CONCEPTS_PER_BATCH} "
            "important concepts and at most "
            f"{IngestionService.MAX_RELATIONSHIPS_PER_BATCH} relationships from this batch."
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
                "nodes": {
                    "type": "array",
                    "items": concept,
                    "maxItems": IngestionService.MAX_CONCEPTS_PER_BATCH,
                },
                "relationships": {
                    "type": "array",
                    "items": relationship,
                    "maxItems": IngestionService.MAX_RELATIONSHIPS_PER_BATCH,
                },
            },
            "required": ["nodes", "relationships"],
            "additionalProperties": False,
        }

    @staticmethod
    def _resolve_embedding_device() -> str:
        from app.core.model_runtime import local_model_device
        return local_model_device()

    @staticmethod
    def _validate_llm_configured() -> None:
        provider = settings.llm_provider.lower()
        if provider == "groq" and not settings.groq_api_key:
            if not cerebras_service.configured:
                raise LLMConfigurationError(
                    "GROQ_API_KEY or CEREBRAS_API_KEY is required when LLM_PROVIDER=groq"
                )
        if provider == "cerebras" and not cerebras_service.configured:
            raise LLMConfigurationError(
                "CEREBRAS_API_KEY is required when LLM_PROVIDER=cerebras"
            )
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
                        batch_key=cls._graph_batch_key(
                            section_key,
                            section_chunks[offset : offset + batch_size],
                        ),
                        section_key=section_key,
                        section_label=section_label,
                        chunks=tuple(section_chunks[offset : offset + batch_size]),
                    )
                )
        return batches

    @classmethod
    def _graph_batch_key(
        cls,
        section_key: str,
        chunks: Sequence[DocumentChunk],
    ) -> str:
        return cls._checkpoint_key("section:v2", section_key, chunks)

    @staticmethod
    def _graph_provider_route() -> list[str]:
        if settings.llm_provider.lower() != "groq":
            return [settings.llm_provider.lower()]
        providers = ["groq"] if settings.groq_api_key else []
        if settings.gemini_api_key:
            providers.append("gemini")
        elif cerebras_service.configured:
            providers.append("cerebras")
        return providers

    @classmethod
    def _checkpoint_key(
        cls, kind: str, section_key: str, chunks: Sequence[DocumentChunk],
        *, recovery_context: str = "",
    ) -> str:
        models = {
            "groq": settings.groq_model,
            "gemini": settings.gemini_model,
            "cerebras": settings.cerebras_model,
        }
        route = cls._graph_provider_route()
        contract = {
            "version": cls.GRAPH_CHECKPOINT_CONTRACT_VERSION,
            "route": [(provider, models.get(provider)) for provider in route],
            "cerebras_endpoint": settings.cerebras_base_url if "cerebras" in route else None,
            "prompt": cls._extraction_system_prompt(),
            "schema": cls._graph_extraction_schema(),
            "cerebras_schema": cls._cerebras_graph_extraction_schema() if "cerebras" in route else None,
            "repair_enabled": settings.graph_json_repair_enabled,
            "context_limits": cls.GRAPH_CONTEXT_LIMITS,
            "output_tokens": cls.MAX_GRAPH_OUTPUT_TOKENS,
            "section": section_key,
            "sources": [
                {"excerpt": cls._format_graph_excerpt(chunk),
                 "upload_id": str(chunk.metadata.get("upload_id") or "")}
                for chunk in chunks
            ],
            "recovery_context": recovery_context,
        }
        encoded = json.dumps(contract, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return f"{kind}:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"

    @classmethod
    def decode_graph_checkpoint(cls, payload: dict) -> GraphExtractionResponse:
        # Check raw endpoints/types/size before Pydantic's normalizer can discard
        # bad relationships and make corrupted saved JSON look successful.
        cls._parse_provider_graph(json.dumps({
            "nodes": payload["nodes"], "relationships": payload["relationships"],
        }))
        return GraphExtractionResponse.model_validate(payload)

    @classmethod
    def _validated_checkpoint(
        cls, extraction: GraphExtractionResponse | None, batch: GraphSectionBatch,
    ) -> GraphExtractionResponse | None:
        """Recheck persisted output and bind source metadata to this execution."""
        if extraction is None or (not extraction.nodes and not extraction.empty_extraction):
            return None
        if extraction.provider_limited or extraction.provider_retry_after_seconds is not None:
            return None
        try:
            checked = cls.decode_graph_checkpoint(extraction.model_dump(mode="json"))
        except (GraphStructureError, TypeError, ValueError):
            logger.warning("Ignored invalid persisted graph batch")
            return None
        source_ids = {chunk.id for chunk in batch.chunks}
        if any(node.source_chunk_id not in source_ids for node in checked.nodes):
            logger.warning("Ignored persisted graph batch with mismatched sources")
            return None
        rebound = cls._attach_provenance(checked, batch.chunks)
        return rebound.model_copy(update={"batch_diagnostics": extraction.batch_diagnostics})

    @classmethod
    def _relationship_recovery_request(
        cls, batches: Sequence[GraphSectionBatch], nodes: Sequence[ConceptNode],
    ) -> tuple[GraphSectionBatch, str] | None:
        batch = cls._global_linking_batch(batches)
        if batch is None:
            return None
        context = cls._relationship_recovery_context(batch.chunks, nodes)
        batch = replace(batch, batch_key=cls._checkpoint_key(
            "global:v5", batch.section_key, batch.chunks, recovery_context=context))
        return batch, context

    @classmethod
    def recovery_checkpoint_batch(
        cls, chunks: Sequence[DocumentChunk], completed: Mapping[str, GraphExtractionResponse],
    ) -> GraphSectionBatch | None:
        """Reconstruct the exact recovery identity from saved section evidence.

        Ignore global/unversioned/invalid rows; they cannot authorize themselves.
        Uses the same order, merge and request builder as actual extraction.
        """
        if not settings.graph_global_linking_enabled:
            return None
        batches, partials = [], []
        for batch in cls._graph_section_batches(chunks, batch_size=settings.graph_batch_size):
            extraction = cls._validated_checkpoint(completed.get(batch.batch_key), batch)
            if extraction is not None and extraction.nodes:
                batches.append(batch)
                partials.append(extraction)
        merged = cls._merge_graph_extractions(partials, sections_total=0, sections_succeeded=0,
            sections_failed=0, batches_total=0, batches_succeeded=0, batches_failed=0,
            batches_skipped=0, provider_limited=False, extraction_budget_applied=False, failed_section_labels=[])
        if len(merged.nodes) < 2 or (
            len({batch.section_key for batch in batches}) < 2
            and len(merged.relationships) >= minimum_graph_relationships(len(merged.nodes))
        ):
            return None
        recovery = cls._relationship_recovery_request(batches, merged.nodes)
        return recovery[0] if recovery is not None else None

    @classmethod
    def _relationship_recovery_context(
        cls, chunks: Sequence[DocumentChunk], nodes: Sequence[ConceptNode],
    ) -> str:
        source_chunk_ids = {chunk.id for chunk in chunks}
        candidate_names = [
            node.name for node in nodes if node.source_chunk_id in source_chunk_ids
        ][:cls.MAX_CONCEPTS_PER_BATCH]
        if not candidate_names:
            candidate_names = [node.name for node in nodes[:cls.MAX_CONCEPTS_PER_BATCH]]
        return (
            "Relationship recovery pass. These concepts were already extracted "
            "from the source excerpts: "
            + ", ".join(candidate_names)
            + ". Return a complete graph JSON with both nodes and relationships; "
            "include every relationship endpoint in the nodes array. "
            "Return only relationships directly supported by the excerpts. "
            "Use the allowed relationship types and exact source chunk IDs. "
            "Do not connect concepts merely because they share a section.\n\n"
            + "\n\n".join(cls._format_graph_excerpt(chunk) for chunk in chunks)
        )

    @classmethod
    def _global_linking_batch(
        cls,
        completed_batches: Sequence[GraphSectionBatch],
    ) -> GraphSectionBatch | None:
        """Select bounded evidence from distinct sections or one long section."""

        first_by_section: dict[str, GraphSectionBatch] = {}
        for batch in completed_batches:
            if batch.kind == "section" and batch.chunks:
                first_by_section.setdefault(batch.section_key, batch)
        representatives = list(first_by_section.values())
        if len(representatives) >= 2:
            positions = cls._evenly_spaced_positions(
                len(representatives),
                min(cls.MAX_CONCEPTS_PER_BATCH, len(representatives)),
            )
            selected = [representatives[position].chunks[0] for position in positions]
        else:
            unique_chunks = list(dict.fromkeys(
                chunk.id for batch in completed_batches for chunk in batch.chunks
            ))
            chunks_by_id = {
                chunk.id: chunk for batch in completed_batches for chunk in batch.chunks
            }
            if not unique_chunks:
                return None
            positions = cls._evenly_spaced_positions(
                len(unique_chunks),
                min(cls.MAX_CONCEPTS_PER_BATCH, len(unique_chunks)),
            )
            selected = [chunks_by_id[unique_chunks[position]] for position in positions]
        return GraphSectionBatch(
            # The caller adds the concepts/recovery prompt before cache lookup.
            batch_key=cls._checkpoint_key("global:v5", "__global__", selected),
            section_key="__global__",
            section_label="Relationship recovery",
            chunks=tuple(selected),
            kind="global",
        )

    @staticmethod
    def _select_graph_batches(
        batches: Sequence[GraphSectionBatch],
        limit: int,
    ) -> list[GraphSectionBatch]:
        """Apply a fair free-tier budget across beginning, middle, and end sections."""

        if len(batches) <= limit:
            return list(batches)

        first_batch_by_section: dict[str, GraphSectionBatch] = {}
        for batch in batches:
            first_batch_by_section.setdefault(batch.section_key, batch)
        section_batches = list(first_batch_by_section.values())

        if len(section_batches) >= limit:
            positions = IngestionService._evenly_spaced_positions(
                len(section_batches),
                limit,
            )
            selected = [section_batches[position] for position in positions]
        else:
            selected = list(section_batches)
            selected_ids = {id(batch) for batch in selected}
            remaining = [batch for batch in batches if id(batch) not in selected_ids]
            remaining_slots = limit - len(selected)
            positions = IngestionService._evenly_spaced_positions(
                len(remaining),
                remaining_slots,
            )
            selected.extend(remaining[position] for position in positions)

        original_order = {id(batch): index for index, batch in enumerate(batches)}
        return sorted(selected, key=lambda batch: original_order[id(batch)])

    @staticmethod
    def _evenly_spaced_positions(item_count: int, sample_count: int) -> list[int]:
        if item_count <= 0 or sample_count <= 0:
            return []
        if sample_count >= item_count:
            return list(range(item_count))
        return sorted(
            {
                round(index * (item_count - 1) / max(sample_count - 1, 1))
                for index in range(sample_count)
            }
        )

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
        return GraphExtractionResponse(
            nodes=nodes, relationships=relationships,
            empty_extraction=not extraction.nodes,
        )

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
        batches_skipped: int,
        provider_limited: bool,
        extraction_budget_applied: bool,
        failed_section_labels: list[str],
    ) -> GraphExtractionResponse:
        """Merge section graphs by normalized name with stable first-seen IDs."""

        canonical_nodes: dict[str, ConceptNode] = {}
        used_ids: set[str] = set()
        endpoint_maps: list[dict[str, str]] = []

        for extraction in extractions:
            endpoint_map: dict[str, str] = {}
            for node in extraction.nodes:
                normalized_name = normalize_concept_name(node.name)
                if not normalized_name:
                    continue
                existing = canonical_nodes.get(normalized_name)
                if existing is None:
                    # Provider IDs are unique only within one response. Different
                    # sections frequently reuse IDs such as "1" for unrelated nodes.
                    canonical_id = node.id
                    if canonical_id in used_ids:
                        suffix = hashlib.sha256(normalized_name.encode("utf-8")).hexdigest()[:16]
                        canonical_id = f"{node.id}:{suffix}"
                        while canonical_id in used_ids:
                            canonical_id += ":1"
                    used_ids.add(canonical_id)
                    canonical_nodes[normalized_name] = node.model_copy(
                        update={"id": canonical_id, "normalized_name": normalized_name}
                    )
                    endpoint_map[node.id] = canonical_id
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
            batches_skipped=batches_skipped,
            provider_limited=provider_limited,
            extraction_budget_applied=extraction_budget_applied,
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
