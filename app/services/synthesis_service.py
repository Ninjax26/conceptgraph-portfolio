import asyncio
from typing import Any

from groq import Groq

from app.core.config import settings
from app.core.exceptions import LLMConfigurationError


class SynthesisService:
    def validate_provider_configured(self) -> None:
        provider = settings.llm_provider.lower()
        if provider == "groq" and not settings.groq_api_key:
            raise LLMConfigurationError("GROQ_API_KEY is required when LLM_PROVIDER=groq")
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
            return await asyncio.to_thread(
                self._synthesize_with_groq,
                question,
                graph_context,
                sources,
            )
        raise ValueError(f"Unsupported LLM_PROVIDER: {settings.llm_provider}")

    def _synthesize_with_groq(
        self,
        question: str,
        graph_context: list[dict[str, Any]],
        sources: list[dict[str, Any]],
    ) -> str:
        client = Groq(
            api_key=settings.groq_api_key,
            timeout=settings.provider_timeout_seconds,
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
        return (
            f"Question:\n{question}\n\n"
            f"Graph context:\n{graph_context}\n\n"
            f"Course sources:\n{source_context}"
        )
