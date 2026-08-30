"""Syllabus-Bounded Exam Generator service.

Retrieves text chunks from Qdrant using strict course filtering and instructs
the LLM to generate a multiple-choice exam
purely from the retrieved syllabus content.
"""

import asyncio
import json
import logging
from typing import Any

from groq import (
    APIConnectionError,
    APITimeoutError,
    Groq,
    InternalServerError,
    RateLimitError,
)
from pydantic import BaseModel, ConfigDict, Field, model_validator
from qdrant_client import QdrantClient
from qdrant_client.http.exceptions import UnexpectedResponse
from qdrant_client.models import FieldCondition, Filter, MatchAny

from app.core.config import settings
from app.core.database import qdrant_client as default_qdrant_client
from app.core.exceptions import (
    LLMConfigurationError,
    LLMProviderRateLimitError,
    LLMProviderRequestError,
    LLMProviderUnavailableError,
)
from app.schemas.exam import ExamResponse, ExamSource, MockQuestion
from app.services.course_service import ReadyCourseContext
from app.services.cerebras_service import cerebras_service
from app.services.provider_failover import provider_circuit_breaker

logger = logging.getLogger(__name__)


class GeneratedQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    question_text: str = Field(min_length=1)
    options: list[str] = Field(min_length=4, max_length=4)
    correct_answer: str = Field(min_length=1)
    explanation: str = Field(min_length=1)
    topic: str = Field(min_length=1)
    citation_ids: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_answer(self) -> "GeneratedQuestion":
        if self.correct_answer not in self.options:
            raise ValueError("correct_answer must exactly match one option.")
        return self


class GeneratedExam(BaseModel):
    model_config = ConfigDict(extra="forbid")

    questions: list[GeneratedQuestion] = Field(default_factory=list)


class ExamService:
    """Generates syllabus-bounded mock exams from Qdrant + LLM."""

    def __init__(
        self,
        vector_client: QdrantClient = default_qdrant_client,
    ) -> None:
        self.vector_client = vector_client
        self.collection_name = settings.qdrant_collection_name

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def generate_exam(
        self,
        context: ReadyCourseContext,
        num_questions: int = 5,
    ) -> ExamResponse:
        """End-to-end exam generation pipeline.

        1. Metadata-filter Qdrant for chunks matching the course.
        2. Concatenate the chunk texts into a bounded context window.
        3. Ask the LLM to produce *num_questions* MCQs strictly from that
           context, returning structured JSON conforming to ExamResponse.
        """
        # Step 1 – metadata-filtered retrieval
        chunks = await asyncio.to_thread(
            self._retrieve_chunks_by_metadata,
            context.document_ids,
        )

        if not chunks:
            logger.warning(
                "No chunks found for course_id=%s; returning empty exam.",
                context.course.display_name,
            )
            return ExamResponse(
                course_id=context.course.id,
                questions=[],
                source_count=0,
                coverage={},
            )

        # Step 2 – constrained generation
        exam_sources = self._select_exam_sources(chunks)
        context_text = self._build_context(exam_sources)
        questions = await self._generate_questions(
            context_text=context_text,
            num_questions=num_questions,
            sources=exam_sources,
        )
        if len(questions) != num_questions:
            raise RuntimeError(
                f"Exam generation produced {len(questions)} valid questions; expected {num_questions}."
            )

        used_source_ids = {
            source.source_id
            for question in questions
            for source in question.sources
        }
        return ExamResponse(
            course_id=context.course.id,
            questions=questions,
            source_count=len(used_source_ids),
            coverage=self._build_coverage(questions),
        )

    # ------------------------------------------------------------------
    # Step 1: Metadata-filtered retrieval (no semantic search)
    # ------------------------------------------------------------------

    def _retrieve_chunks_by_metadata(
        self,
        document_ids: list[str],
        batch_size: int = 100,
    ) -> list[dict[str, Any]]:
        """Scroll through Qdrant with a strict metadata filter.

        Returns all matching payloads without a query vector – this is a
        pure filter-based retrieval.
        """
        if not self._collection_exists():
            logger.info("Qdrant collection %s does not exist yet.", self.collection_name)
            return []

        query_filter = Filter(
            must=[
                FieldCondition(
                    key="upload_id",
                    match=MatchAny(any=document_ids),
                ),
            ]
        )

        all_chunks: list[dict[str, Any]] = []
        offset = None

        while True:
            try:
                # qdrant-client >=1.7 exposes `scroll`
                scroll_kwargs: dict[str, Any] = {
                    "collection_name": self.collection_name,
                    "scroll_filter": query_filter,
                    "limit": batch_size,
                    "with_payload": True,
                    "with_vectors": False,
                }
                if offset is not None:
                    scroll_kwargs["offset"] = offset

                points, next_offset = self.vector_client.scroll(**scroll_kwargs)
            except UnexpectedResponse as exc:
                if self._is_qdrant_not_found_error(exc):
                    logger.info("Qdrant collection %s does not exist yet.", self.collection_name)
                    return []
                raise
            except TypeError:
                # Older qdrant-client versions use positional / different kwarg names.
                try:
                    points, next_offset = self.vector_client.scroll(
                        collection_name=self.collection_name,
                        scroll_filter=query_filter,
                        limit=batch_size,
                        with_payload=True,
                    )
                except UnexpectedResponse as exc:
                    if self._is_qdrant_not_found_error(exc):
                        logger.info("Qdrant collection %s does not exist yet.", self.collection_name)
                        return []
                    raise

            for point in points:
                payload = point.payload or {}
                all_chunks.append(
                    {
                        "id": str(point.id),
                        "text": str(payload.get("text", "")),
                        "metadata": {
                            key: value
                            for key, value in payload.items()
                            if key != "text"
                        },
                    }
                )

            if next_offset is None:
                break
            offset = next_offset

        logger.info(
            "Retrieved %d chunks for %d ready documents",
            len(all_chunks),
            len(document_ids),
        )
        return all_chunks

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

    # ------------------------------------------------------------------
    # Step 2: Constrained LLM generation
    # ------------------------------------------------------------------

    async def _generate_questions(
        self,
        context_text: str,
        num_questions: int,
        sources: list[ExamSource],
    ) -> list[MockQuestion]:
        """Dispatch to the configured LLM provider."""
        provider = settings.llm_provider.lower()
        if provider == "gemini":
            return await asyncio.to_thread(
                self._generate_with_gemini, context_text, num_questions, sources,
            )
        if provider == "groq":
            return await self._generate_with_failover(
                context_text, num_questions, sources
            )
        if provider == "cerebras":
            return await asyncio.to_thread(
                self._generate_with_cerebras, context_text, num_questions, sources,
            )
        raise ValueError(f"Unsupported LLM_PROVIDER: {settings.llm_provider}")

    async def _generate_with_failover(
        self,
        context_text: str,
        num_questions: int,
        sources: list[ExamSource],
    ) -> list[MockQuestion]:
        primary_error: Exception | None = None
        if provider_circuit_breaker.is_available("groq") and settings.groq_api_key:
            try:
                return await asyncio.to_thread(
                    self._generate_with_groq, context_text, num_questions, sources,
                )
            except (
                RateLimitError,
                APIConnectionError,
                APITimeoutError,
                InternalServerError,
            ) as exc:
                primary_error = exc
                provider_circuit_breaker.block(
                    "groq", settings.llm_failover_cooldown_seconds
                )
                logger.warning("Groq exam generation is unavailable; trying Cerebras")
            except ValueError as exc:
                primary_error = exc
                logger.warning("Groq returned an invalid exam; trying Cerebras")

        if cerebras_service.configured and provider_circuit_breaker.is_available("cerebras"):
            try:
                return await asyncio.to_thread(
                    self._generate_with_cerebras, context_text, num_questions, sources,
                )
            except (LLMProviderRateLimitError, LLMProviderUnavailableError) as exc:
                provider_circuit_breaker.block(
                    "cerebras", settings.llm_failover_cooldown_seconds
                )
                logger.warning("Cerebras exam generation is temporarily unavailable")
                primary_error = primary_error or exc
            except (LLMProviderRequestError, LLMConfigurationError, ValueError) as exc:
                logger.warning("Cerebras exam failover could not generate a valid exam")
                primary_error = primary_error or exc

        raise LLMProviderUnavailableError(
            "No exam-generation provider is currently available"
        ) from primary_error

    def _generate_with_groq(
        self,
        context_text: str,
        num_questions: int,
        sources: list[ExamSource],
    ) -> list[MockQuestion]:
        if not settings.groq_api_key:
            raise LLMConfigurationError("GROQ_API_KEY is required when LLM_PROVIDER=groq")

        client = Groq(
            api_key=settings.groq_api_key,
            timeout=settings.provider_timeout_seconds,
            max_retries=0,
        )
        completion = client.chat.completions.create(
            model=settings.groq_model,
            messages=[
                {"role": "system", "content": self._system_prompt(num_questions)},
                {"role": "user", "content": self._user_prompt(context_text)},
            ],
            temperature=0,
            response_format={"type": "json_object"},
        )

        raw = completion.choices[0].message.content or "{}"
        questions = self._parse_questions(raw, sources)
        if not questions:
            raise ValueError("Groq returned no valid exam questions")
        return questions

    def _generate_with_cerebras(
        self,
        context_text: str,
        num_questions: int,
        sources: list[ExamSource],
    ) -> list[MockQuestion]:
        raw = cerebras_service.complete(
            [
                {"role": "system", "content": self._system_prompt(num_questions)},
                {"role": "user", "content": self._user_prompt(context_text)},
            ],
            max_tokens=2_500,
            response_format={"type": "json_object"},
        )
        questions = self._parse_questions(raw, sources)
        if not questions:
            raise ValueError("Cerebras returned no valid exam questions")
        return questions

    def _generate_with_gemini(
        self,
        context_text: str,
        num_questions: int,
        sources: list[ExamSource],
    ) -> list[MockQuestion]:
        if not settings.gemini_api_key:
            raise LLMConfigurationError("GEMINI_API_KEY is required when LLM_PROVIDER=gemini")

        import google.generativeai as genai

        genai.configure(api_key=settings.gemini_api_key)
        model = genai.GenerativeModel(settings.gemini_model)
        response = model.generate_content(
            [
                self._system_prompt(num_questions),
                self._user_prompt(context_text),
            ],
            generation_config={
                "temperature": 0,
                "response_mime_type": "application/json",
            },
            request_options={"timeout": settings.provider_timeout_seconds},
        )
        return self._parse_questions(response.text or "{}", sources)

    # ------------------------------------------------------------------
    # Prompt construction
    # ------------------------------------------------------------------

    @staticmethod
    def _system_prompt(num_questions: int) -> str:
        schema = json.dumps(GeneratedExam.model_json_schema(), indent=2)
        return (
            "You are ConceptGraph Exam Generator – a syllabus-bounded academic "
            "assessment engine. Your ONLY job is to produce a strict JSON exam.\n\n"
            "RULES:\n"
            f"1. Generate exactly {num_questions} multiple-choice questions.\n"
            "2. Every question must be STRICTLY derived from the provided text context. "
            "Do NOT use external knowledge.\n"
            "3. Each question must have exactly 4 options.\n"
            "4. The 'correct_answer' field must be one of the 4 options verbatim.\n"
            "5. The 'explanation' must cite specific information from the provided "
            "context that justifies the correct answer.\n"
            "6. Include a concise topic and one or more citation_ids copied exactly from "
            "the provided [Exam Source ...] labels. Never invent a citation ID.\n"
            "7. Spread questions across distinct topics and documents when the context permits.\n"
            "8. Output ONLY a JSON object with a single key 'questions' containing "
            "the list of question objects.\n\n"
            f"Response JSON schema:\n{schema}"
        )

    @staticmethod
    def _user_prompt(context_text: str) -> str:
        return (
            "Generate the exam questions based EXCLUSIVELY on the following "
            "syllabus content. Do not add any information not present in this text.\n\n"
            "--- BEGIN SYLLABUS CONTENT ---\n"
            f"{context_text}\n"
            "--- END SYLLABUS CONTENT ---"
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_context(sources: list[ExamSource]) -> str:
        if not sources:
            return ""
        return "\n\n".join(
            f"[Exam Source {source.source_id}] | {source.document_name} | "
            f"page {source.page_number or 'unknown'} | "
            f"section {source.section_heading or 'unknown'}\n"
            f"{source.supporting_passage}"
            for source in sources
        )

    @staticmethod
    def _select_exam_sources(
        chunks: list[dict[str, Any]],
        limit: int = 12,
    ) -> list[ExamSource]:
        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        seen: set[tuple[str, int | None, str]] = set()
        for chunk in chunks:
            metadata = chunk.get("metadata") or {}
            passage = " ".join(str(chunk.get("text", "")).split())
            if not passage:
                continue
            document_name = str(metadata.get("document_name") or "Course PDF")
            page = (
                metadata.get("page_number")
                if isinstance(metadata.get("page_number"), int)
                else None
            )
            heading = str(metadata.get("section_heading") or "").strip()
            dedupe_key = (document_name, page, passage[:240])
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            topic_key = heading.casefold() if heading else f"page-{page or 'unknown'}"
            groups.setdefault((document_name, topic_key), []).append(chunk)

        ordered_groups = [
            sorted(
                group,
                key=lambda item: (
                    int((item.get("metadata") or {}).get("page_number") or 0),
                    int((item.get("metadata") or {}).get("chunk_index") or 0),
                ),
            )
            for _, group in sorted(groups.items())
        ]
        selected: list[dict[str, Any]] = []
        while ordered_groups and len(selected) < limit:
            remaining: list[list[dict[str, Any]]] = []
            for group in ordered_groups:
                if len(selected) >= limit:
                    break
                selected.append(group.pop(0))
                if group:
                    remaining.append(group)
            ordered_groups = remaining

        sources: list[ExamSource] = []
        for index, chunk in enumerate(selected, start=1):
            metadata = chunk.get("metadata") or {}
            page = (
                metadata.get("page_number")
                if isinstance(metadata.get("page_number"), int)
                else None
            )
            sources.append(
                ExamSource(
                    source_id=f"exam-source-{index}",
                    document_name=str(metadata.get("document_name") or "Course PDF"),
                    page_number=page,
                    section_heading=str(metadata.get("section_heading") or "") or None,
                    supporting_passage=" ".join(
                        str(chunk.get("text", "")).split()
                    )[:900],
                )
            )
        return sources

    @staticmethod
    def _build_coverage(
        questions: list[MockQuestion],
    ) -> dict[str, list[str] | dict[str, list[int]]]:
        topics = sorted({question.topic for question in questions})
        documents = sorted(
            {
                source.document_name
                for question in questions
                for source in question.sources
            }
        )
        pages_by_document: dict[str, list[int]] = {}
        for question in questions:
            for source in question.sources:
                if source.page_number is not None:
                    pages_by_document.setdefault(source.document_name, []).append(
                        source.page_number
                    )
        return {
            "topics": topics,
            "documents": documents,
            "pages_by_document": {
                document: sorted(set(pages))
                for document, pages in pages_by_document.items()
            },
        }

    @staticmethod
    def _parse_questions(
        raw_json: str,
        sources: list[ExamSource],
    ) -> list[MockQuestion]:
        """Parse LLM JSON output into validated MockQuestion objects."""
        try:
            data = json.loads(raw_json)
        except json.JSONDecodeError as exc:
            logger.error("LLM returned invalid JSON: %s", exc)
            raise RuntimeError("LLM returned invalid exam JSON.") from exc

        if isinstance(data, list):
            data = {"questions": data}
        try:
            generated = GeneratedExam.model_validate(data)
        except Exception as exc:
            raise RuntimeError("LLM returned an unexpected exam JSON structure.") from exc

        sources_by_id = {source.source_id: source for source in sources}
        questions: list[MockQuestion] = []
        for item in generated.questions:
            try:
                cited_sources = [
                    sources_by_id[source_id]
                    for source_id in dict.fromkeys(item.citation_ids)
                    if source_id in sources_by_id
                ]
                if not cited_sources:
                    raise ValueError("Question did not cite a valid exam source.")
                questions.append(
                    MockQuestion(
                        question_text=item.question_text,
                        options=item.options,
                        correct_answer=item.correct_answer,
                        explanation=item.explanation,
                        topic=item.topic,
                        sources=cited_sources,
                    )
                )
            except Exception as exc:
                logger.warning("Skipping malformed question: %s", exc)
                continue

        return questions
