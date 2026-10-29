"""Unscheduled, read-only rollout evidence. A scan never certifies writer drain."""
from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import re

from qdrant_client.models import FieldCondition, Filter, MatchValue

from app.core.blocking import run_blocking_safely
from app.services.upload_service import UploadService
from app.services.vector_cleanup_service import VectorCleanupScope


@dataclass(frozen=True)
class VectorReadContract:
    collection_name: str
    store_sha256: str
    embedding_sha256: str
    dimension: int
    scoped: bool

    def __post_init__(self):
        if (not isinstance(self.collection_name, str) or not self.collection_name.strip()
            or len(self.collection_name) > 255 or type(self.dimension) is not int or self.dimension < 1
            or type(self.scoped) is not bool
            or not all(isinstance(v, str) and re.fullmatch(r"[0-9a-f]{64}", v)
                       for v in (self.store_sha256, self.embedding_sha256))):
            raise ValueError("Invalid vector read contract.")

    @classmethod
    def from_settings(cls, config):
        if (type(config.embedding_dimension) is not int or config.embedding_dimension < 1
            or type(config.graph_projection_fencing_enabled) is not bool
            or not all(isinstance(v, str) and v.strip() for v in
                       (config.embedding_provider, config.embedding_model_name))):
            raise ValueError("Invalid vector read contract.")
        scope = VectorCleanupScope.from_url(config.qdrant_url, config.qdrant_collection_name)
        embedding = json.dumps([config.embedding_provider, config.embedding_model_name], separators=(",", ":"))
        return cls(scope.collection_name, scope.store_sha256, hashlib.sha256(embedding.encode()).hexdigest(),
                   config.embedding_dimension, config.graph_projection_fencing_enabled)


@dataclass(frozen=True)
class ReadyVectorIdentity:
    upload_id: str
    task_id: str
    course_id: str
    expected_chunks: int
    known_tasks: frozenset[str]

    def __post_init__(self):
        if (not all(isinstance(v, str) and v.strip() for v in (self.upload_id, self.task_id, self.course_id))
            or type(self.expected_chunks) is not int or self.expected_chunks < 1
            or not isinstance(self.known_tasks, frozenset) or self.task_id not in self.known_tasks
            or not all(isinstance(v, str) and v.strip() for v in self.known_tasks)):
            raise ValueError("A READY audit requires an explicit source execution and attempt history.")

    @classmethod
    def from_record(cls, record, *, known_tasks):
        if record.stage != "READY" or UploadService.is_deleting(record):
            raise ValueError("Only non-deleting READY records can be audited.")
        return cls(record.upload_id, record.task_id, record.course_uuid,
                   record.processed_chunk_count, frozenset(known_tasks))


async def audit_ready_vectors(client, contract: VectorReadContract, identity: ReadyVectorIdentity, *,
                              peer_contract: VectorReadContract, page_size=128, max_points=4096):
    """Inspect one bounded metadata snapshot, without fetching text or vectors.

    Contracts are caller-supplied observations, not live fleet attestation. Known
    tasks must come from the same source's PostgreSQL attempt history. A cursor
    completing under concurrent writes does not establish a stable snapshot.
    """
    if (not isinstance(contract, VectorReadContract) or not isinstance(peer_contract, VectorReadContract)
        or not isinstance(identity, ReadyVectorIdentity)):
        raise ValueError("Use validated vector contracts and a READY identity.")
    if (type(page_size) is not int or not 1 <= page_size <= 256
        or type(max_points) is not int or not 1 <= max_points <= 100000):
        raise ValueError("Invalid vector audit bounds.")
    counts = dict.fromkeys(("scanned", "current_points", "current_unique_chunks", "known_stale_points",
        "unknown_task_points", "untagged_points", "foreign_points", "malformed_current_points",
        "duplicate_current_chunks"), 0)
    result = {"scan_completed": False, "read_coverage_ok": False, "cleanup_payloads_trackable": False,
        "activation_allowed": False, "writer_drain_verified": False, "error_type": None, "counts": counts}
    if contract != peer_contract or not contract.scoped:
        result["error_type"] = "VectorContractMismatch"
        return result
    seen_chunks, offsets, offset = set(), set(), None
    try:
        collection = await run_blocking_safely(client.get_collection, contract.collection_name)
        vectors = collection.config.params.vectors
        if (type(getattr(vectors, "size", None)) is not int or vectors.size != contract.dimension
            or str(getattr(vectors, "distance", "")).casefold() != "cosine"):
            result["error_type"] = "VectorSchemaMismatch"
            return result
        while counts["scanned"] < max_points:
            limit = min(page_size, max_points-counts["scanned"])
            points, next_offset = await run_blocking_safely(client.scroll,
                collection_name=contract.collection_name,
                scroll_filter=Filter(must=[FieldCondition(key="upload_id", match=MatchValue(value=identity.upload_id))]),
                offset=offset, limit=limit, with_vectors=False,
                with_payload=["upload_id", "course_id", "execution_token", "chunk_id", "page_number"])
            if (len(points) > limit or (next_offset is not None and
                (not points or type(next_offset) not in (int, str) or next_offset in offsets))):
                result["error_type"] = "InvalidPagination"
                return result
            for point in points:
                counts["scanned"] += 1
                payload = point.payload if isinstance(point.payload, Mapping) else {}
                if payload.get("upload_id") != identity.upload_id or payload.get("course_id") != identity.course_id:
                    counts["foreign_points"] += 1
                elif not isinstance(payload.get("execution_token"), str) or not payload["execution_token"].strip():
                    counts["untagged_points"] += 1
                elif payload["execution_token"] != identity.task_id:
                    counts["known_stale_points" if payload["execution_token"] in identity.known_tasks
                           else "unknown_task_points"] += 1
                else:
                    counts["current_points"] += 1
                    chunk, page = payload.get("chunk_id"), payload.get("page_number")
                    if not isinstance(chunk, str) or not chunk.strip() or type(page) is not int or page < 1:
                        counts["malformed_current_points"] += 1
                    elif chunk in seen_chunks:
                        counts["duplicate_current_chunks"] += 1
                    else:
                        seen_chunks.add(chunk)
                        counts["current_unique_chunks"] += 1
            if next_offset is None:
                result["scan_completed"] = True
                break
            offsets.add(next_offset)
            offset = next_offset
    except Exception as error:
        result["error_type"] = type(error).__name__
        return result
    clean_current = not any(counts[key] for key in
        ("foreign_points", "malformed_current_points", "duplicate_current_chunks"))
    result["read_coverage_ok"] = (result["scan_completed"] and clean_current
                                 and counts["current_unique_chunks"] == identity.expected_chunks)
    result["cleanup_payloads_trackable"] = (result["scan_completed"] and clean_current
        and counts["untagged_points"] == counts["unknown_task_points"] == 0)
    return result
