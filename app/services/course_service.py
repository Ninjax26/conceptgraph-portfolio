from dataclasses import dataclass
from datetime import datetime
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.processing import GraphStatus, ProcessingStage, assess_graph_status, normalize_course_name
from app.models.document_upload import Course, DocumentUpload


class CourseNotFoundError(LookupError):
    pass


class CourseNotReadyError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ReadyCourseContext:
    course: Course
    documents: tuple[DocumentUpload, ...]

    @property
    def document_ids(self) -> list[str]:
        return [document.upload_id for document in self.documents]

    @property
    def graph_course_ids(self) -> list[str]:
        aliases = {self.course.id, self.course.display_name}
        aliases.update(document.course_id for document in self.documents)
        return sorted(aliases)

    @property
    def graph_status(self) -> str:
        return _aggregate_graph_status(self.documents)


@dataclass(frozen=True, slots=True)
class CourseSummary:
    course: Course
    total_documents: int
    active_documents: int
    ready_documents: int
    failed_documents: int
    processed_chunk_count: int
    graph_node_count: int
    graph_edge_count: int
    graph_status: str | None
    last_updated_at: datetime | None
    historical_records: int
    duplicate_records: int


class CourseService:
    async def list_summaries(self, session: AsyncSession) -> list[CourseSummary]:
        courses = list((await session.execute(select(Course).order_by(Course.display_name))).scalars())
        documents = list(
            (
                await session.execute(
                    select(DocumentUpload).order_by(DocumentUpload.updated_at.desc())
                )
            ).scalars()
        )
        summaries: list[CourseSummary] = []
        status_rank = {"ready": 0, "active": 1, "failed": 2, "cancelled": 3}
        for course in courses:
            records = [document for document in documents if document.course_uuid == course.id]
            canonical: dict[str, DocumentUpload] = {}
            for document in sorted(
                records,
                key=lambda item: (status_rank.get(item.status, 9), -item.updated_at.timestamp()),
            ):
                canonical.setdefault(document.content_hash or document.upload_id, document)
            logical_documents = list(canonical.values())
            ready_documents = [
                document
                for document in logical_documents
                if document.stage == ProcessingStage.READY.value
            ]
            summaries.append(
                CourseSummary(
                    course=course,
                    total_documents=len(logical_documents),
                    active_documents=sum(document.status == "active" for document in logical_documents),
                    ready_documents=len(ready_documents),
                    failed_documents=sum(document.status == "failed" for document in logical_documents),
                    processed_chunk_count=sum(
                        document.processed_chunk_count for document in ready_documents
                    ),
                    graph_node_count=sum(
                        document.graph_node_count for document in ready_documents
                    ),
                    graph_edge_count=sum(
                        document.graph_edge_count for document in ready_documents
                    ),
                    graph_status=(
                        _aggregate_graph_status(tuple(ready_documents))
                        if ready_documents
                        else None
                    ),
                    last_updated_at=max((document.updated_at for document in records), default=None),
                    historical_records=len(records),
                    duplicate_records=len(records) - len(logical_documents),
                )
            )
        return summaries

    async def get_or_create(self, session: AsyncSession, name: str) -> Course:
        normalized = normalize_course_name(name)
        if not normalized:
            raise ValueError("Course name cannot be empty.")
        existing = await self.resolve(session, name, required=False)
        if existing is not None:
            return existing
        course = Course(
            id=str(uuid5(NAMESPACE_URL, f"conceptgraph:course:{normalized}")),
            normalized_name=normalized,
            display_name=name.strip().upper(),
        )
        session.add(course)
        await session.flush()
        return course

    async def resolve(
        self,
        session: AsyncSession,
        name_or_id: str,
        *,
        required: bool = True,
    ) -> Course | None:
        normalized = normalize_course_name(name_or_id)
        result = await session.execute(
            select(Course).where(
                (Course.id == name_or_id.strip()) | (Course.normalized_name == normalized)
            )
        )
        course = result.scalar_one_or_none()
        if course is None and required:
            raise CourseNotFoundError("Course not found.")
        return course

    async def get_ready_context(
        self,
        session: AsyncSession,
        name_or_id: str,
    ) -> ReadyCourseContext:
        course = await self.resolve(session, name_or_id)
        assert course is not None
        result = await session.execute(
            select(DocumentUpload)
            .where(
                DocumentUpload.course_uuid == course.id,
                DocumentUpload.stage == ProcessingStage.READY.value,
                DocumentUpload.processed_chunk_count > 0,
            )
            .order_by(DocumentUpload.created_at.desc())
        )
        unique_documents: dict[str, DocumentUpload] = {}
        for document in result.scalars().all():
            unique_documents.setdefault(document.content_hash or document.upload_id, document)
        documents = tuple(unique_documents.values())
        if not documents:
            raise CourseNotReadyError(
                "This course has no ready documents. Finish processing a PDF before continuing."
            )
        return ReadyCourseContext(course=course, documents=documents)


def _document_graph_status(document: DocumentUpload) -> str:
    stored_status = getattr(document, "graph_status", None)
    if stored_status in {status.value for status in GraphStatus}:
        return stored_status
    return assess_graph_status(
        int(getattr(document, "graph_node_count", 0)),
        int(getattr(document, "graph_edge_count", 0)),
    ).value


def _aggregate_graph_status(documents: tuple[DocumentUpload, ...]) -> str:
    statuses = {_document_graph_status(document) for document in documents}
    if statuses == {GraphStatus.GRAPH_READY.value}:
        return GraphStatus.GRAPH_READY.value
    if statuses == {GraphStatus.READY_WITHOUT_GRAPH.value}:
        return GraphStatus.READY_WITHOUT_GRAPH.value
    return GraphStatus.GRAPH_PARTIAL.value
