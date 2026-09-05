import logging
from functools import lru_cache
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel, ConfigDict, Field

from app.core.exceptions import LLMConfigurationError
from app.services.rag_service import RetrievalMode, RetrievalService
from app.services.rerank_service import RerankService
from app.services.synthesis_service import SynthesisService
from app.services.citation_service import assess_evidence, build_sources
from app.services.course_service import CourseNotFoundError, CourseNotReadyError, CourseService
from app.core.database import get_postgres_session


router = APIRouter(prefix="/api/v1", tags=["query"])
logger = logging.getLogger(__name__)


class QueryRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    question: str = Field(..., min_length=1)
    course_id: str = Field(..., min_length=1)
    retrieval_mode: RetrievalMode = RetrievalMode.TWO_HOP


class AnswerConfidence(BaseModel):
    level: Literal["high", "medium", "low", "insufficient"]
    score: float = Field(ge=0, le=1)
    evidence_count: int = Field(ge=0)
    reason: str


class QueryResponse(BaseModel):
    answer: str
    sources: list[dict[str, Any]]
    graph_context: list[dict[str, Any]]
    graph_metadata: dict[str, Any]
    confidence: AnswerConfidence


@lru_cache
def get_retrieval_service() -> RetrievalService:
    return RetrievalService()


@lru_cache
def get_rerank_service() -> RerankService:
    return RerankService()


@lru_cache
def get_synthesis_service() -> SynthesisService:
    return SynthesisService()


@router.post("/query", response_model=QueryResponse)
async def query_conceptgraph(
    request: QueryRequest,
    retrieval_service: RetrievalService = Depends(get_retrieval_service),
    db: AsyncSession = Depends(get_postgres_session),
) -> QueryResponse:
    try:
        course_context = await CourseService().get_ready_context(db, request.course_id)
    except CourseNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CourseNotReadyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    try:
        retrieval_result = await retrieval_service.retrieve(
            question=request.question,
            context=course_context,
            retrieval_mode=request.retrieval_mode,
        )
    except Exception as exc:
        logger.exception("Retrieval pipeline failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Course retrieval is temporarily unavailable. Please try again.",
        ) from exc
    if not retrieval_result["chunks"]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Ready document records exist, but their indexed chunks are unavailable. "
                "Reprocess the affected document."
            ),
        )

    rerank_service = get_rerank_service()
    try:
        ranked_chunks = await rerank_service.rerank(
            request.question,
            retrieval_result["chunks"],
        )
    except Exception as exc:
        logger.exception("Reranking pipeline failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Result ranking is temporarily unavailable. Please try again.",
        ) from exc
    evidence_chunks, confidence = assess_evidence(ranked_chunks)
    sources = build_sources(evidence_chunks)
    if not sources:
        return QueryResponse(
            answer="I could not find enough reliable course content to answer this confidently.",
            sources=[],
            graph_context=retrieval_result["graph_context"],
            graph_metadata=retrieval_result["graph_metadata"],
            confidence=confidence,
        )
    synthesis_service = get_synthesis_service()
    try:
        answer = await synthesis_service.synthesize(
            question=request.question,
            graph_context=(retrieval_result["graph_context"]
                if retrieval_result["graph_metadata"].get("filter_reason") == "query_subgraph" else []),
            sources=sources,
        )
    except LLMConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"LLM provider is not configured: {exc}",
        ) from exc
    except Exception as exc:
        logger.exception("Answer synthesis failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Answer generation is temporarily unavailable. Please try again.",
        ) from exc

    return QueryResponse(
        answer=answer,
        sources=sources,
        graph_context=retrieval_result["graph_context"],
        graph_metadata=retrieval_result["graph_metadata"],
        confidence=confidence,
    )
