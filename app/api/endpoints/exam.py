"""API endpoint for the Syllabus-Bounded Exam Generator."""

from functools import lru_cache
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel, ConfigDict, Field

from app.core.exceptions import (
    LLMConfigurationError,
    LLMProviderRateLimitError,
    LLMProviderRequestError,
    LLMProviderUnavailableError,
)
from app.schemas.exam import ExamResponse
from app.services.exam_service import ExamService
from app.services.course_service import CourseNotFoundError, CourseNotReadyError, CourseService
from app.core.database import get_postgres_session

router = APIRouter(prefix="/api/v1/exam", tags=["exam"])
logger = logging.getLogger(__name__)


class ExamGenerateRequest(BaseModel):
    """Request body for the exam generation endpoint."""

    model_config = ConfigDict(str_strip_whitespace=True)

    course_id: str = Field(..., min_length=1, description="Identifier for the course/document.")
    num_questions: int = Field(
        default=5,
        ge=1,
        le=20,
        description="Number of MCQs to generate (1-20).",
    )


@lru_cache
def get_exam_service() -> ExamService:
    return ExamService()


@router.post(
    "/generate",
    response_model=ExamResponse,
    summary="Generate a syllabus-bounded mock exam",
    description=(
        "Retrieves text chunks matching the given course_id "
        "from the Qdrant vector store via metadata filtering, then instructs "
        "the LLM to produce a structured multiple-choice exam strictly from "
        "that content."
    ),
)
async def generate_exam(
    request: ExamGenerateRequest,
    exam_service: ExamService = Depends(get_exam_service),
    db: AsyncSession = Depends(get_postgres_session),
) -> ExamResponse:
    try:
        context = await CourseService().get_ready_context(db, request.course_id)
    except CourseNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CourseNotReadyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    try:
        result = await exam_service.generate_exam(
            context=context,
            num_questions=request.num_questions,
        )
        if not result.questions:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Ready document records exist, but their indexed chunks are unavailable. "
                    "Reprocess the affected document."
                ),
            )
    except LLMConfigurationError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"LLM provider is not configured: {exc}",
        ) from exc
    except (
        LLMProviderRateLimitError,
        LLMProviderRequestError,
        LLMProviderUnavailableError,
    ) as exc:
        raise HTTPException(
            status_code=503,
            detail="AI exam providers are temporarily unavailable. Please try again later.",
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Exam generation failed")
        raise HTTPException(
            status_code=500,
            detail="Exam generation failed. Please try again in a moment.",
        ) from exc

    return result
