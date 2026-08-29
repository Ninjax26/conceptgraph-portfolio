from __future__ import annotations

import asyncio
import logging
from functools import lru_cache
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.endpoints.ingest import build_pdf_response
from app.core.config import settings
from app.core.database import get_postgres_session
from app.services.course_service import CourseNotFoundError, CourseNotReadyError, CourseService
from app.services.rag_service import RetrievalService
from app.services.security_service import demo_access_service, rate_limit_service
from app.services.storage_service import ObjectNotFoundError, ObjectStorageError, storage_service

router = APIRouter(prefix="/api/v1/public", tags=["public-demo"])
logger = logging.getLogger(__name__)


class PublicSampleDocument(BaseModel):
    upload_id: str
    filename: str
    page_preview_url: str


class PublicSampleResponse(BaseModel):
    course_id: str
    course_name: str
    graph_status: str
    documents: list[PublicSampleDocument]
    graph_context: list[dict[str, Any]]
    graph_metadata: dict[str, Any]


@lru_cache
def get_public_retrieval_service() -> RetrievalService:
    return RetrievalService()


async def _apply_public_rate_limit(request: Request, response: Response) -> None:
    client_host = request.client.host if request.client else "unknown"
    fingerprint = demo_access_service.fingerprint(client_host)
    result = await rate_limit_service.check(
        f"public-sample:{fingerprint}",
        settings.rate_limit_requests_per_minute,
    )
    response.headers["X-RateLimit-Limit"] = str(result.limit)
    response.headers["X-RateLimit-Remaining"] = str(result.remaining)
    if not result.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Public sample request limit reached. Please try again shortly.",
            headers={"Retry-After": str(result.retry_after)},
        )


async def _get_sample_context(db: AsyncSession):
    if not settings.public_sample_course_id:
        raise HTTPException(
            status_code=404,
            detail="The public sample course has not been configured.",
        )
    try:
        return await CourseService().get_ready_context(
            db,
            settings.public_sample_course_id,
        )
    except (CourseNotFoundError, CourseNotReadyError) as exc:
        raise HTTPException(
            status_code=404,
            detail="The public sample course is not available yet.",
        ) from exc


@router.get("/sample", response_model=PublicSampleResponse)
async def get_public_sample(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_postgres_session),
    retrieval_service: RetrievalService = Depends(get_public_retrieval_service),
) -> PublicSampleResponse:
    await _apply_public_rate_limit(request, response)
    context = await _get_sample_context(db)
    try:
        graph = await retrieval_service.fetch_course_graph(context)
    except Exception as exc:
        logger.exception("Public sample graph retrieval failed")
        raise HTTPException(
            status_code=503,
            detail="The public sample graph is temporarily unavailable.",
        ) from exc

    return PublicSampleResponse(
        course_id=context.course.id,
        course_name=context.course.display_name,
        graph_status=context.graph_status,
        documents=[
            PublicSampleDocument(
                upload_id=document.upload_id,
                filename=document.original_filename,
                page_preview_url=(
                    f"/api/v1/public/sample/uploads/{document.upload_id}/preview"
                ),
            )
            for document in context.documents
        ],
        graph_context=graph.concepts,
        graph_metadata=graph.metadata,
    )


@router.get("/sample/uploads/{upload_id}/preview")
async def preview_public_sample_upload(
    upload_id: str,
    request: Request,
    response: Response,
    range_header: str | None = Header(default=None, alias="Range"),
    db: AsyncSession = Depends(get_postgres_session),
) -> Response:
    await _apply_public_rate_limit(request, response)
    context = await _get_sample_context(db)
    document = next(
        (item for item in context.documents if item.upload_id == upload_id),
        None,
    )
    if document is None:
        raise HTTPException(status_code=404, detail="Sample document not found.")
    try:
        content = await asyncio.to_thread(storage_service.get_bytes, document.storage_key)
    except ObjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Sample PDF is no longer available.") from exc
    except ObjectStorageError as exc:
        raise HTTPException(
            status_code=503,
            detail="Sample PDF storage is temporarily unavailable.",
        ) from exc
    pdf_response = build_pdf_response(content, document.original_filename, range_header)
    pdf_response.headers.update(
        {
            "X-RateLimit-Limit": response.headers["X-RateLimit-Limit"],
            "X-RateLimit-Remaining": response.headers["X-RateLimit-Remaining"],
        }
    )
    return pdf_response
