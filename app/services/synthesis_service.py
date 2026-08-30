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

from app.core.config import settings
from app.core.exceptions import (
    LLMConfigurationError,
    LLMProviderRateLimitError,
    LLMProviderUnavailableError,
)
from app.services.cerebras_service import cerebras_service
from app.services.provider_failover import provider_circuit_breaker


logger = logging.getLogger(__name__)


class SynthesisService:
    MAX_GRAPH_CONTEXT_CHARS = 6_000

    def validate_provider_configured(self) -> None:
        provider = settings.llm_provider.lower()
        if provider == "groq" and not settings.groq_api_key and not cerebras_service.configured:
            raise LLMConfigurationError(
                "GROQ_API_KEY or CEREBRAS_API_KEY is required when LLM_PROVIDER=groq"
            )
        if provider == "cerebras" and not cerebras_service.configured:
            raise LLMConfigurationError(
                "CEREBRAS_API_KEY is required when LLM_PROVIDER=cerebras"
            )
        if provider == "gemini" and not settings.gemini_api_key:
            raise LLMConfigurationError("GEMINI_API_KEY is required when LLM_PROVIDER=gemini")

    async def synthesize(
        self,
        question: str,
        graph_context: list[dict[str, Any]],
        sources: list[dict[str, Any]],
    ) -> str:
        self.validate_provider_configured()
        provider = settings.llm_provider.lower()
        if provider == "gemini":
            return await asyncio.to_thread(
                self._synthesize_with_gemini,
                question,
                graph_context,
                sources,
            )
        if provider == "groq":
            return await self._synthesize_with_failover(
                question, graph_context, sources
            )
        if provider == "cerebras":
            try:
                return await asyncio.to_thread(
                    self._synthesize_with_cerebras,
                    question,
                    graph_context,
                    sources,
                )
            except (LLMProviderRateLimitError, LLMProviderUnavailableError):
                logger.warning(
                    "Cerebras answer synthesis is unavailable; returning retrieved evidence"
                )
                return self._grounded_evidence_fallback(sources)
        raise ValueError(f"Unsupported LLM_PROVIDER: {settings.llm_provider}")

    async def _synthesize_with_failover(
        self,
        question: str,
        graph_context: list[dict[str, Any]],
        sources: list[dict[str, Any]],
    ) -> str:
        if provider_circuit_breaker.is_available("groq") and settings.groq_api_key:
            try:
                return await asyncio.to_thread(
                    self._synthesize_with_groq,
                    question,
                    graph_context,
                    sources,
                )
            except (
                RateLimitError,
                APIConnectionError,
                APITimeoutError,
                InternalServerError,
            ):
                provider_circuit_breaker.block(
                    "groq", settings.llm_failover_cooldown_seconds
                )
                logger.warning(
                    "Groq answer synthesis is temporarily unavailable; trying Cerebras"
                )

        if cerebras_service.configured and provider_circuit_breaker.is_available("cerebras"):
            try:
                return await asyncio.to_thread(
                    self._synthesize_with_cerebras,
                    question,
                    graph_context,
                    sources,
                )
            except (LLMProviderRateLimitError, LLMProviderUnavailableError):
                provider_circuit_breaker.block(
                    "cerebras", settings.llm_failover_cooldown_seconds
                )
                logger.warning("Cerebras answer synthesis is temporarily unavailable")
            except LLMConfigurationError as exc:
                logger.warning("Cerebras answer failover is misconfigured: %s", exc)

        logger.warning(
            "All answer synthesis providers are unavailable; returning retrieved evidence"
        )
        return self._grounded_evidence_fallback(sources)

    def _synthesize_with_groq(
        self,
        question: str,
        graph_context: list[dict[str, Any]],
        sources: list[dict[str, Any]],
    ) -> str:
        client = Groq(
            api_key=settings.groq_api_key,
            timeout=settings.provider_timeout_seconds,
            max_retries=0,
        )
        completion = client.chat.completions.create(
            model=settings.groq_model,
            messages=[
                {"role": "system", "content": self._system_prompt()},
                {"role": "user", "content": self._user_prompt(question, graph_context, sources)},
            ],
            temperature=0,
        )
        return completion.choices[0].message.content or ""

    def _synthesize_with_cerebras(
        self,
        question: str,
        graph_context: list[dict[str, Any]],
        sources: list[dict[str, Any]],
    ) -> str:
        return cerebras_service.complete(
            [
                {"role": "system", "content": self._system_prompt()},
                {
                    "role": "user",
                    "content": self._user_prompt(question, graph_context, sources),
                },
            ],
            max_tokens=1_200,
        )

    @staticmethod
    def _grounded_evidence_fallback(sources: list[dict[str, Any]]) -> str:
        passages: list[str] = []
        for index, source in enumerate(sources[:2], start=1):
            passage = " ".join(str(source.get("supporting_passage") or "").split())
            if not passage:
                continue
            if len(passage) > 600:
                passage = f"{passage[:597].rsplit(' ', 1)[0]}..."
            page = source.get("page_number")
            citation = f"[Source {index}{f', p. {page}' if page else ''}]"
            passages.append(f"- {passage} {citation}")
        if not passages:
            return "I could not find enough reliable course content to answer this confidently."
        return (
            "AI synthesis is temporarily unavailable, so here are the most relevant "
            "retrieved course passages:\n\n"
            + "\n\n".join(passages)
        )

    def _synthesize_with_gemini(
        self,
        question: str,
        graph_context: list[dict[str, Any]],
        sources: list[dict[str, Any]],
    ) -> str:
        import google.generativeai as genai

        genai.configure(api_key=settings.gemini_api_key)
        model = genai.GenerativeModel(settings.gemini_model)
        response = model.generate_content(
            [
                self._system_prompt(),
                self._user_prompt(question, graph_context, sources),
            ],
            generation_config={"temperature": 0},
            request_options={"timeout": settings.provider_timeout_seconds},
        )
        return response.text or ""

    @staticmethod
    def _system_prompt() -> str:
        return (
            "You are ConceptGraph, a syllabus-bounded academic assistant. Answer strictly "
            "from the provided textbook chunks and graph context. Do not use outside knowledge. "
            "Cite evidence using only readable labels such as [Source 1] or [Source 2, p. 6]. "
            "Never mention chunk IDs, UUIDs, vector IDs, database IDs, file paths, or scores. "
            "If evidence is insufficient, state exactly: \"I could not find enough reliable "
            "course content to answer this confidently.\""
        )

    @staticmethod
    def _user_prompt(
        question: str,
        graph_context: list[dict[str, Any]],
        sources: list[dict[str, Any]],
    ) -> str:
        source_context = "\n\n".join(
            f"[Source {index}] | {source['document_name']} | "
            f"page {source.get('page_number') or 'unknown'} | "
            f"section {source.get('section_heading') or 'unknown'}\n"
            f"{source['supporting_passage']}"
            for index, source in enumerate(sources, start=1)
        )
        compact_graph_context = json.dumps(
            graph_context,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )[: SynthesisService.MAX_GRAPH_CONTEXT_CHARS]
        return (
            f"Question:\n{question}\n\n"
            f"Graph context (bounded):\n{compact_graph_context}\n\n"
            f"Course sources:\n{source_context}"
        )
