"""Installation-level resource accounting; callers hold the admission lock."""

from dataclasses import dataclass

from sqlalchemy import case, func, or_, select, text

from app.core.config import Settings, settings
from app.models.document_upload import DocumentUpload


class ResourceBudgetExceeded(ValueError):
    def __init__(self, message: str, *, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True)
class ResourceUsage:
    documents: int = 0
    pages: int = 0
    chunks: int = 0
    source_bytes: int = 0
    pending: int = 0
    unknown_documents: int = 0


class ResourceBudgetService:
    def __init__(self, config: Settings = settings):
        self.config = config

    async def lock(self, session):
        await session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                              {"key": "conceptgraph:pdf-admission"})

    def chunk_reservation(self, pages):
        return min(self.config.max_chunks_per_document, pages * self.config.chunk_reservation_per_page)

    async def usage(self, session, *, exclude_upload_id=None):
        c = self.config
        # Unknown legacy records are counted conservatively, never as free capacity.
        query = select(
            func.count(DocumentUpload.upload_id),
            func.coalesce(func.sum(func.coalesce(DocumentUpload.page_count, c.max_pages_per_document)), 0),
            func.coalesce(func.sum(func.coalesce(DocumentUpload.reserved_chunk_count,
                                               func.nullif(DocumentUpload.processed_chunk_count, 0), c.max_chunks_per_document)), 0),
            func.coalesce(func.sum(func.coalesce(DocumentUpload.source_bytes, c.max_pdf_size_mb * 1024 * 1024)), 0),
            func.coalesce(func.sum(case((DocumentUpload.status == "active", 1), else_=0)), 0),
            func.coalesce(func.sum(case((or_(DocumentUpload.page_count.is_(None),
                                            DocumentUpload.source_bytes.is_(None),
                                            (DocumentUpload.reserved_chunk_count.is_(None) &
                                             (DocumentUpload.processed_chunk_count == 0))), 1), else_=0)), 0),
        )
        if exclude_upload_id is not None:
            query = query.where(DocumentUpload.upload_id != exclude_upload_id)
        row = (await session.execute(query)).one()
        return ResourceUsage(*(int(value) for value in row))

    def check(self, usage, *, pages, chunks, source_bytes, new_document=True):
        c = self.config
        if pages > c.max_pages_per_document or chunks > c.max_chunks_per_document:
            raise ResourceBudgetExceeded("This PDF exceeds the configured per-document page or chunk budget.", retryable=False)
        limits = [(usage.pages + pages, c.max_pages_per_installation, "page"),
                  (usage.chunks + chunks, c.max_chunks_per_installation, "chunk"),
                  (usage.source_bytes + source_bytes, c.max_storage_mb * 1024 * 1024, "source storage")]
        if new_document:
            limits.append((usage.pending + 1, c.max_pending_documents, "pending processing"))
            if c.max_pdfs_per_installation:
                limits.append((usage.documents + 1, c.max_pdfs_per_installation, "legacy PDF count"))
        for required, maximum, resource in limits:
            if required > maximum:
                if resource == "pending processing":
                    raise ResourceBudgetExceeded("The pending processing budget is full. Wait for jobs to finish or for an ingestion worker to return.")
                raise ResourceBudgetExceeded(f"The installation's {resource} budget is full. Remove an eligible document or adjust its configured budget.")


resource_budget_service = ResourceBudgetService()
