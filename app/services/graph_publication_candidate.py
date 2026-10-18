"""Pure publication preflight; not a cross-store commit or authority check."""

from collections.abc import Sequence
from dataclasses import dataclass
import hashlib
import json

from app.schemas.extraction import ALLOWED_RELATIONSHIP_TYPES, GraphExtractionResponse
from app.services.parser_service import DocumentChunk


@dataclass(frozen=True, slots=True)
class GraphPublicationCandidate:
    payload_json: str
    sha256: str


def prepare_graph_publication(extraction: GraphExtractionResponse, chunks: Sequence[DocumentChunk], *,
                              upload_id: str, course_id: str, document_task_id: str,
                              document_name: str) -> GraphPublicationCandidate:
    """Freeze structurally valid, source-bound output for a future publisher.

    Caller must obtain chunks from the fenced snapshot reader and recheck job /
    document authority at publication time. This does not assess semantic truth.
    """
    identity = {"upload_id": upload_id, "document_id": course_id,
                "execution_token": document_task_id, "document_name": document_name}
    if not all(isinstance(value, str) and value.strip() for value in identity.values()):
        raise ValueError("Publication requires a complete document identity.")
    sources = {}
    for chunk in chunks:
        if not chunk.id or chunk.id in sources or any(chunk.metadata.get(key) != value for key, value in identity.items()):
            raise ValueError("Publication contains foreign or duplicate source chunks.")
        page = chunk.metadata.get("page_number")
        if type(page) is not int or page < 1:
            raise ValueError("Publication source requires a valid page.")
        sources[chunk.id] = chunk
    if not sources:
        raise ValueError("Publication requires persisted source chunks.")
    if any(type(node.page_number) is not int or any(type(page) is not int for page in node.page_numbers)
           for node in extraction.nodes):
        raise ValueError("Publication page numbers must be integers.")
    raw = extraction.model_dump(mode="json")
    if any(edge["relation_type"] not in ALLOWED_RELATIONSHIP_TYPES for edge in raw["relationships"]):
        raise ValueError("Publication contains an unsupported relationship.")
    checked = GraphExtractionResponse.model_validate(raw).model_dump(mode="json")
    if checked != raw:
        raise ValueError("Publication graph must already be normalized and validated.")
    for node in extraction.nodes:
        source = sources.get(node.source_chunk_id)
        if (source is None or node.upload_id != upload_id or node.document_name != document_name
                or node.page_number != source.metadata["page_number"]):
            raise ValueError("Publication concept has mismatched primary provenance.")
        expected_heading = str(source.metadata.get("section_heading") or "Unlabelled section")
        allowed_headings = {expected_heading} if source.metadata.get("section_heading") else {"", expected_heading}
        if node.section_heading not in allowed_headings:
            raise ValueError("Publication concept has a mismatched primary section.")
        references = node.source_chunk_ids or [node.source_chunk_id]
        if node.source_chunk_id not in references or any(key not in sources for key in references):
            raise ValueError("Publication concept references foreign source chunks.")
        if node.page_numbers and set(node.page_numbers) != {sources[key].metadata["page_number"] for key in references}:
            raise ValueError("Publication concept has mismatched source pages.")
        if node.section_headings and set(node.section_headings) != {
                str(sources[key].metadata.get("section_heading") or "Unlabelled section") for key in references}:
            raise ValueError("Publication concept has mismatched source sections.")
    # Presentation order and runtime counters are intentionally included: this
    # fingerprints the exact candidate, not semantic equivalence between graphs.
    source_payload = json.dumps([{"id": chunk.id, "text": chunk.text, "metadata": chunk.metadata}
                                 for chunk in chunks], sort_keys=True, separators=(",", ":"),
                                ensure_ascii=False, allow_nan=False)
    source_sha256 = hashlib.sha256(source_payload.encode("utf-8")).hexdigest()
    payload = json.dumps({"identity": identity, "source_sha256": source_sha256, "extraction": raw}, sort_keys=True,
                         separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return GraphPublicationCandidate(payload, hashlib.sha256(payload.encode("utf-8")).hexdigest())
