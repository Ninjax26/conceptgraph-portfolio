from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Course(Base):
    __tablename__ = "courses"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    normalized_name: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class DocumentUpload(Base):
    __tablename__ = "document_uploads"

    upload_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    course_id: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    course_uuid: Mapped[str | None] = mapped_column(String(64), ForeignKey("courses.id"), index=True, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    week_number: Mapped[int] = mapped_column(nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    # Keep the existing SQL column name for backward compatibility while the
    # application treats its value as an opaque object-storage key.
    storage_key: Mapped[str] = mapped_column("stored_file_path", Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), index=True, nullable=False, default="queued")
    stage: Mapped[str] = mapped_column(String(32), index=True, nullable=False, default="UPLOADED")
    failure_category: Mapped[str | None] = mapped_column(String(32), nullable=True)
    retryable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    last_attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True, nullable=True)
    processed_chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reserved_chunk_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    graph_node_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    graph_edge_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    graph_status: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ProcessingAttempt(Base):
    __tablename__ = "processing_attempts"
    __table_args__ = (UniqueConstraint("document_id", "attempt_number"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    document_id: Mapped[str] = mapped_column(String(64), ForeignKey("document_uploads.upload_id", ondelete="CASCADE"), index=True, nullable=False)
    task_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    stage: Mapped[str] = mapped_column(String(32), nullable=False)
    failure_category: Mapped[str | None] = mapped_column(String(32), nullable=True)
    retryable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class GraphExtractionCheckpoint(Base):
    """Durable, per-batch LLM output used to resume graph construction."""

    __tablename__ = "graph_extraction_checkpoints"
    __table_args__ = (UniqueConstraint("upload_id", "batch_key"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    upload_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("document_uploads.upload_id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    batch_key: Mapped[str] = mapped_column(String(80), nullable=False)
    section_key: Mapped[str] = mapped_column(String(512), nullable=False)
    section_label: Mapped[str] = mapped_column(String(512), nullable=False)
    extraction_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class WorkerPresence(Base):
    __tablename__ = "worker_presence"

    instance_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    concurrency: Mapped[int] = mapped_column(Integer, nullable=False)


class GraphContinuationJob(Base):
    """Independent graph lease; never repurposes the READY evidence task token."""

    __tablename__ = "graph_continuation_jobs"
    __table_args__ = (
        CheckConstraint("status IN ('pending', 'running', 'stopped', 'completed')", name="ck_graph_job_status"),
        CheckConstraint("attempt_count >= 0 AND attempt_count <= max_attempts AND max_attempts BETWEEN 1 AND 8", name="ck_graph_job_attempts"),
        CheckConstraint("(status = 'running' AND claim_token IS NOT NULL AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL) OR "
                        "(status <> 'running' AND claim_token IS NULL AND lease_owner IS NULL AND lease_expires_at IS NULL)", name="ck_graph_job_lease"),
        Index("ix_graph_job_due", "status", "next_attempt_at"),
        Index("ix_graph_job_lease", "status", "lease_expires_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    upload_id: Mapped[str] = mapped_column(String(64), ForeignKey("document_uploads.upload_id", ondelete="CASCADE"), unique=True, nullable=False)
    document_task_id: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    snapshot_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    contract_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    claim_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failure_category: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class GraphPublicationDraft(Base):
    """Saved candidate only; never proof that a remote projection committed."""

    __tablename__ = "graph_publication_drafts"
    __table_args__ = (
        CheckConstraint("length(payload_sha256) = 64", name="ck_graph_draft_digest"),
        CheckConstraint("octet_length(payload_json) BETWEEN 1 AND 8388608", name="ck_graph_draft_bytes"),
    )

    job_id: Mapped[str] = mapped_column(String(64), ForeignKey("graph_continuation_jobs.id", ondelete="CASCADE"), primary_key=True)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class GraphPublicationIntent(Base):
    """Durable breadcrumb for a future remote write; survives source deletion."""

    __tablename__ = "graph_publication_intents"
    __table_args__ = (
        UniqueConstraint("job_id"),
        CheckConstraint("length(payload_sha256) = 64", name="ck_graph_intent_digest"),
        Index("ix_graph_publication_intent_upload", "upload_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # Deliberately no cascading foreign keys: an in-flight remote commit may
    # outlive deletion of its document or graph job and still need cleanup.
    job_id: Mapped[str] = mapped_column(String(64), nullable=False)
    upload_id: Mapped[str] = mapped_column(String(64), nullable=False)
    document_task_id: Mapped[str] = mapped_column(String(64), nullable=False)
    course_id: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_generation: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class GraphPublicationReceipt(Base):
    """Observed remote marker identity; survives source deletion for reconciliation."""

    __tablename__ = "graph_publication_receipts"
    __table_args__ = (CheckConstraint("length(payload_sha256) = 64", name="ck_graph_receipt_digest"),)

    # No cascading foreign keys: the marker can outlive its upload and job.
    intent_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[str] = mapped_column(String(64), nullable=False)
    upload_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    course_id: Mapped[str] = mapped_column(String(64), nullable=False)
    document_task_id: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_generation: Mapped[int | None] = mapped_column(Integer, nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class GraphPublicationCleanupReceipt(Base):
    """Retained proof that a deleted source's remote tombstone completed."""

    __tablename__ = "graph_publication_cleanup_receipts"

    # No source/intent foreign key: both may be absent during recovery.
    intent_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    upload_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    course_id: Mapped[str] = mapped_column(String(64), nullable=False)
    cleaned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ProviderBudget(Base):
    __tablename__ = "provider_budgets"

    scope: Mapped[str] = mapped_column(String(80), primary_key=True)
    provider: Mapped[str] = mapped_column(String(16), primary_key=True)
    minute_window: Mapped[int] = mapped_column(BigInteger, nullable=False)
    day_window: Mapped[int] = mapped_column(BigInteger, nullable=False)
    minute_requests: Mapped[int] = mapped_column(Integer, nullable=False)
    day_requests: Mapped[int] = mapped_column(Integer, nullable=False)
    minute_estimated_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    day_estimated_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
