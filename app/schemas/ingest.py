from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.core.processing import GraphStatus


class IngestResponse(BaseModel):
    message: str
    task_id: str
    upload_id: str
    course_id: str
    course_name: str
    original_filename: str
    status: str = "UPLOADED"
    duplicate: bool = False
    preview_url: str


class UploadStatusResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    upload_id: str
    task_id: str
    course_id: str
    course_name: str
    original_filename: str
    status: str
    stage: str
    failure_category: str | None = None
    retryable: bool = False
    attempt_count: int = 1
    last_attempted_at: datetime | None = None
    last_heartbeat_at: datetime | None = None
    processed_chunk_count: int = 0
    graph_node_count: int = 0
    graph_edge_count: int = 0
    graph_status: GraphStatus | None = None
    error_message: str | None = None
    result_json: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    preview_url: str = Field(default="")


class CourseSummaryResponse(BaseModel):
    course_id: str
    course_name: str
    total_documents: int = 0
    active_documents: int = 0
    ready_documents: int = 0
    failed_documents: int = 0
    processed_chunk_count: int = 0
    graph_node_count: int = 0
    graph_edge_count: int = 0
    graph_status: GraphStatus | None = None
    last_updated_at: datetime | None = None
    historical_records: int = 0
    duplicate_records: int = 0
