from collections.abc import Mapping, Sequence
from typing import Any

import httpx

from app.core.config import settings
from app.core.exceptions import (
    LLMConfigurationError,
    LLMProviderRateLimitError,
    LLMProviderRequestError,
    LLMProviderUnavailableError,
)


class CerebrasService:
    """Small OpenAI-compatible Cerebras client without another SDK dependency."""

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client

    @property
    def configured(self) -> bool:
        return bool(settings.cerebras_api_key_value)

    def complete(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        response_format: dict[str, Any] | None = None,
        max_tokens: int = 1_000,
    ) -> str:
        api_key = settings.cerebras_api_key_value
        if not api_key:
            raise LLMConfigurationError("CEREBRAS_API_KEY is not configured")

        payload: dict[str, Any] = {
            "model": settings.cerebras_model,
            "messages": [dict(message) for message in messages],
            "temperature": 0,
            "max_completion_tokens": max_tokens,
        }
        if response_format is not None:
            payload["response_format"] = response_format

        try:
            if self._client is not None:
                response = self._client.post(
                    "/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json=payload,
                )
            else:
                with httpx.Client(
                    base_url=settings.cerebras_base_url.rstrip("/"),
                    timeout=settings.provider_timeout_seconds,
                ) as client:
                    response = client.post(
                        "/chat/completions",
                        headers={"Authorization": f"Bearer {api_key}"},
                        json=payload,
                    )
        except httpx.TimeoutException as exc:
            raise LLMProviderUnavailableError("Cerebras request timed out") from exc
        except httpx.RequestError as exc:
            raise LLMProviderUnavailableError("Cerebras could not be reached") from exc

        if response.status_code in {401, 403}:
            raise LLMConfigurationError("CEREBRAS_API_KEY was rejected")
        if response.status_code == 429:
            raise LLMProviderRateLimitError("Cerebras quota is temporarily exhausted")
        if response.status_code >= 500:
            raise LLMProviderUnavailableError("Cerebras is temporarily unavailable")
        if response.status_code >= 400:
            raise LLMProviderRequestError(
                f"Cerebras rejected the request with HTTP {response.status_code}"
            )

        try:
            body = response.json()
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise LLMProviderUnavailableError(
                "Cerebras returned an invalid completion response"
            ) from exc
        if not isinstance(content, str) or not content.strip():
            raise LLMProviderUnavailableError("Cerebras returned an empty completion")
        return content


cerebras_service = CerebrasService()
