import asyncio
import math
from typing import Any

import httpx

from app.core.config import settings


class RerankService:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client
        self._model = None

    async def rerank(
        self,
        query: str,
        chunks: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if not chunks:
            return []
        if settings.rerank_provider == "cohere":
            return await self._rerank_with_cohere(query, chunks)
        return await asyncio.to_thread(self._rerank_locally, query, chunks)

    async def _rerank_with_cohere(
        self,
        query: str,
        chunks: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        api_key = settings.cohere_api_key_value
        if not api_key:
            raise RuntimeError("COHERE_API_KEY is required when RERANK_PROVIDER=cohere.")
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(
            timeout=settings.provider_timeout_seconds
        )
        try:
            response = await client.post(
                "https://api.cohere.com/v2/rerank",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "X-Client-Name": "conceptgraph-portfolio",
                },
                json={
                    "model": settings.rerank_model_name,
                    "query": query,
                    "documents": [str(chunk.get("text", "")) for chunk in chunks],
                    "top_n": len(chunks),
                },
            )
            response.raise_for_status()
            results = response.json().get("results", [])
        finally:
            if owns_client:
                await client.aclose()

        ranked_chunks: list[dict[str, Any]] = []
        seen: set[int] = set()
        for result in results:
            index = int(result["index"])
            if index < 0 or index >= len(chunks) or index in seen:
                raise RuntimeError("Cohere returned an invalid rerank result index.")
            seen.add(index)
            probability = max(
                1e-6,
                min(1.0 - 1e-6, float(result["relevance_score"])),
            )
            ranked_chunk = dict(chunks[index])
            # Citation scoring applies a sigmoid to local cross-encoder logits.
            # Convert the hosted probability to a logit to preserve that contract.
            ranked_chunk["rerank_score"] = math.log(probability / (1.0 - probability))
            ranked_chunks.append(ranked_chunk)
        if len(ranked_chunks) != len(chunks):
            raise RuntimeError("Cohere returned an incomplete rerank response.")
        return ranked_chunks

    def _rerank_locally(
        self,
        query: str,
        chunks: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        pairs = [(query, str(chunk.get("text", ""))) for chunk in chunks]
        scores = self.model.predict(pairs)
        ranked_chunks: list[dict[str, Any]] = []
        for chunk, score in zip(chunks, scores, strict=True):
            ranked_chunk = dict(chunk)
            ranked_chunk["rerank_score"] = float(score)
            ranked_chunks.append(ranked_chunk)
        return sorted(
            ranked_chunks,
            key=lambda chunk: float(chunk["rerank_score"]),
            reverse=True,
        )

    @property
    def model(self):
        if self._model is None:
            try:
                from sentence_transformers import CrossEncoder
            except ImportError as exc:
                raise RuntimeError(
                    "Local reranking requires requirements-local-models.txt."
                ) from exc
            self._model = CrossEncoder(
                settings.rerank_model_name,
                device=self._resolve_device(),
            )
        return self._model

    @staticmethod
    def _resolve_device() -> str:
        import torch

        if torch.backends.mps.is_available():
            return "mps"
        return "cpu"
