from collections.abc import Sequence

import httpx

from app.core.config import settings
from app.core.exceptions import (
    LLMConfigurationError,
    LLMProviderRateLimitError,
    LLMProviderRequestError,
    LLMProviderUnavailableError,
)
from app.services.provider_failover import retry_after_seconds


class GeminiService:
    """Minimal Gemini REST client used by direct and failover generation paths."""

    BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client

    @property
    def configured(self) -> bool:
        return bool(settings.gemini_api_key)

    def generate(
        self,
        contents: Sequence[str],
        *,
        json_mode: bool = False,
        max_output_tokens: int = 1_200,
    ) -> str:
        if not settings.gemini_api_key:
            raise LLMConfigurationError("GEMINI_API_KEY is not configured")

        generation_config: dict[str, object] = {
            "temperature": 0,
            "maxOutputTokens": max_output_tokens,
        }
        if json_mode:
            generation_config["responseMimeType"] = "application/json"
        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": "\n\n".join(contents)}],
                }
            ],
            "generationConfig": generation_config,
        }
        path = f"/models/{settings.gemini_model}:generateContent"
        headers = {"x-goog-api-key": settings.gemini_api_key}
        try:
            if self._client is not None:
                response = self._client.post(path, headers=headers, json=payload)
            else:
                with httpx.Client(
                    base_url=self.BASE_URL,
                    timeout=settings.provider_timeout_seconds,
                ) as client:
                    response = client.post(path, headers=headers, json=payload)
        except httpx.TimeoutException as exc:
            raise LLMProviderUnavailableError("Gemini request timed out") from exc
        except httpx.RequestError as exc:
            raise LLMProviderUnavailableError("Gemini could not be reached") from exc

        if response.status_code in {401, 403}:
            raise LLMConfigurationError("GEMINI_API_KEY was rejected")
        if response.status_code == 404:
            raise LLMConfigurationError("The configured Gemini model is unavailable")
        if response.status_code == 429:
            raise LLMProviderRateLimitError(
                "Gemini quota is temporarily exhausted",
                retry_after_seconds=retry_after_seconds(response.headers),
            )
        if response.status_code >= 500:
            raise LLMProviderUnavailableError("Gemini is temporarily unavailable")
        if response.status_code >= 400:
            raise LLMProviderRequestError(
                f"Gemini rejected the request with HTTP {response.status_code}"
            )

        try:
            body = response.json()
            parts = body["candidates"][0]["content"]["parts"]
            content = "".join(part.get("text", "") for part in parts)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise LLMProviderUnavailableError(
                "Gemini returned an invalid completion response"
            ) from exc
        if not content.strip():
            raise LLMProviderUnavailableError("Gemini returned an empty completion")
        return content


gemini_service = GeminiService()
