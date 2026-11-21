"""Optional course-scoped reuse of validated, source-neutral graph requests."""

from dataclasses import dataclass, replace
import hashlib
import json
from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.orm import aliased

from app.core.exceptions import GraphStructureError
from app.models.document_upload import DocumentUpload, GraphBatchCacheRow
from app.services.parser_service import DocumentChunk

if TYPE_CHECKING:
    from app.services.ingestion_service import GraphSectionBatch, IngestionService


MAX_ENTRIES_PER_UPLOAD = 48
MAX_PAYLOAD_BYTES = 16 * 1024
NODE_FIELDS = {"id", "name", "type", "description", "source_chunk_id"}


def _digest(key, payload):
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_PAYLOAD_BYTES:
        raise ValueError("Graph batch exceeds cache size limit.")
    return hashlib.sha256(key.encode("ascii") + b"\0" + encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class CacheEntry:
    key: str
    payload: dict
    digest: str


class GraphBatchCache:
    def __init__(self, sessions, upload_id: str):
        self.sessions = sessions
        self.upload_id = upload_id
        self.hits = 0

    async def _read(self, key):
        recipient = aliased(DocumentUpload)
        async with self.sessions() as session:
            row = (await session.execute(select(GraphBatchCacheRow).join(
                DocumentUpload, DocumentUpload.upload_id == GraphBatchCacheRow.upload_id
            ).join(recipient, recipient.course_uuid == DocumentUpload.course_uuid).where(
                recipient.upload_id == self.upload_id,
                DocumentUpload.upload_id != self.upload_id,
                DocumentUpload.status == "ready", DocumentUpload.stage == "READY",
                func.coalesce(DocumentUpload.result_json["deletion_requested"].as_boolean(), False).is_(False),
                GraphBatchCacheRow.task_id == DocumentUpload.task_id,
                GraphBatchCacheRow.cache_key == key,
            ).order_by(GraphBatchCacheRow.upload_id).limit(1))).scalar_one_or_none()
            return CacheEntry(key, row.payload, row.digest) if row else None

    async def extract(self, service: "IngestionService", batch: "GraphSectionBatch", context: str):
        # Preserve the exact semantic headers, excerpt order, and recovery
        # prologue. File identity and chunk IDs are local to the request.
        excerpts = "\n\n".join(service._format_graph_excerpt(chunk) for chunk in batch.chunks)
        if not context.endswith(excerpts):
            raise ValueError("Graph cache requires the exact batch request.")
        prefix = context[:-len(excerpts)]
        neutral = tuple(DocumentChunk(f"source-{i}", chunk.text, {
            "document_name": "source", "page_number": chunk.metadata.get("page_number"),
            "section_heading": chunk.metadata.get("section_heading"),
        }) for i, chunk in enumerate(batch.chunks, start=1))
        key = service._checkpoint_key("shared:v1", batch.kind, neutral, recovery_context=prefix)
        sources = {local.id: original.id for local, original in zip(neutral, batch.chunks, strict=True)}
        entry = await self._read(key)
        graph = None
        if entry:
            try:
                if _digest(key, entry.payload) != entry.digest:
                    raise ValueError("Cache digest mismatch.")
                graph = service._parse_provider_graph(json.dumps(entry.payload))
                if any(set(node) != NODE_FIELDS for node in entry.payload["nodes"]):
                    raise ValueError("Unexpected cached provenance.")
                if any(node.source_chunk_id not in sources for node in graph.nodes):
                    raise ValueError("Unknown cached source.")
            except (GraphStructureError, KeyError, ValueError, TypeError):
                graph = None
        if graph is None:
            request = prefix + "\n\n".join(service._format_graph_excerpt(chunk) for chunk in neutral)
            graph = await service.extract_graph_from_text(request)
            entry = None
            if (not graph.provider_limited and graph.provider_retry_after_seconds is None
                    and (graph.nodes or graph.empty_extraction)
                    and all(node.source_chunk_id in sources for node in graph.nodes)):
                payload = {"nodes": [node.model_dump(include=NODE_FIELDS) for node in graph.nodes],
                           "relationships": [edge.model_dump() for edge in graph.relationships]}
                try:
                    digest = _digest(key, payload)
                    service._parse_provider_graph(json.dumps(payload))
                except (GraphStructureError, ValueError, TypeError):
                    pass
                else:
                    entry = CacheEntry(key, payload, digest)
        else:
            self.hits += 1
        nodes = [node.model_copy(update={"source_chunk_id": sources.get(node.source_chunk_id, "")})
                 for node in graph.nodes]
        return graph.model_copy(update={"nodes": nodes}), replace(batch, cache_entry=entry)


async def save_cache_entry(session, owner: DocumentUpload, entry: CacheEntry) -> None:
    """Caller holds the current upload lock and commits its checkpoint atomically."""
    key = (owner.upload_id, entry.key)
    row = await session.get(GraphBatchCacheRow, key)
    if row is None:
        count = await session.scalar(select(func.count()).select_from(GraphBatchCacheRow).where(
            GraphBatchCacheRow.upload_id == owner.upload_id))
        if count >= MAX_ENTRIES_PER_UPLOAD:
            return
        row = GraphBatchCacheRow(upload_id=owner.upload_id, cache_key=entry.key)
        session.add(row)
    row.task_id = owner.task_id
    row.payload = entry.payload
    row.digest = entry.digest
