import asyncio
from collections.abc import Iterable
import math
from typing import Any

import httpx

from app.core.config import settings
from app.services.retrieval_candidates import rerank_passages
from app.services.evidence_facets import comparison_facets


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
        facets = comparison_facets(query)
        if not facets:
            return await self._score_with_cohere(query, chunks)
        groups = [await self._score_with_cohere(facet, chunks) for facet in facets]
        return self._tag_facets(groups)

    async def _score_with_cohere(
        self, query: str, chunks: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        api_key = settings.cohere_api_key_value
        if not api_key:
            raise RuntimeError("COHERE_API_KEY is required when RERANK_PROVIDER=cohere.")
        owns_client = self._client is None
        windows = self._windows(query, chunks)
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
                    "documents": [passage for _, passage in windows],
                    "top_n": len(windows),
                },
            )
            response.raise_for_status()
            results = response.json().get("results", [])
        finally:
            if owns_client:
                await client.aclose()

        scores: dict[int, float] = {}
        seen: set[int] = set()
        for result in results:
            index = result["index"]
            if type(index) is not int:
                raise RuntimeError("Cohere returned an invalid rerank result index.")
            if index < 0 or index >= len(windows) or index in seen:
                raise RuntimeError("Cohere returned an invalid rerank result index.")
            seen.add(index)
            relevance = float(result["relevance_score"])
            if not math.isfinite(relevance) or not 0 <= relevance <= 1:
                raise RuntimeError("Cohere returned an invalid relevance score.")
            probability = max(
                1e-6,
                min(1.0 - 1e-6, relevance),
            )
            # Citation scoring applies a sigmoid to local cross-encoder logits.
            # Convert the hosted probability to a logit to preserve that contract.
            scores[index] = math.log(probability / (1.0 - probability))
        if len(scores) != len(windows):
            raise RuntimeError("Cohere returned an incomplete rerank response.")
        return self._rank_windows(chunks, windows, [scores[index] for index in range(len(windows))])

    def _rerank_locally(
        self,
        query: str,
        chunks: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        facets = comparison_facets(query)
        queries = facets or [query]
        plans = [self._windows(part, chunks) for part in queries]
        pairs = [(part, passage) for part, windows in zip(queries, plans, strict=True)
                 for _, passage in windows]
        scores = list(self.model.predict(pairs))
        if len(scores) != len(pairs):
            raise ValueError("The local reranker returned an incomplete score batch.")
        offset, groups = 0, []
        for windows in plans:
            groups.append(self._rank_windows(chunks, windows, scores[offset:offset + len(windows)]))
            offset += len(windows)
        return self._tag_facets(groups) if facets else groups[0]

    @staticmethod
    def _tag_facets(groups: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
        return sorted([dict(chunk, evidence_facet=index, evidence_facet_count=len(groups))
                       for index, group in enumerate(groups) for chunk in group],
                      key=lambda chunk: float(chunk["rerank_score"]), reverse=True)

    @staticmethod
    def _windows(query: str, chunks: list[dict[str, Any]]) -> list[tuple[int, str]]:
        return [(index, passage) for index, chunk in enumerate(chunks)
                for passage in rerank_passages(str(chunk.get("text", "")), query)]

    @staticmethod
    def _rank_windows(
        chunks: list[dict[str, Any]],
        windows: list[tuple[int, str]],
        scores: Iterable[float],
    ) -> list[dict[str, Any]]:
        best: dict[int, dict[str, Any]] = {}
        for (index, passage), score in zip(windows, scores, strict=True):
            if not math.isfinite(float(score)):
                raise RuntimeError("The reranker returned an invalid score.")
            if index in best and best[index]["rerank_score"] >= float(score):
                continue
            ranked_chunk = dict(chunks[index])
            ranked_chunk["rerank_score"] = float(score)
            ranked_chunk["rerank_passage"] = passage
            best[index] = ranked_chunk
        return sorted(
            best.values(),
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
        from app.core.model_runtime import local_model_device
        return local_model_device()
