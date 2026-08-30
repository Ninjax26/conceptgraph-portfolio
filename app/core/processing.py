from enum import StrEnum


class ProcessingStage(StrEnum):
    UPLOADED = "UPLOADED"
    EXTRACTING = "EXTRACTING"
    EXTRACTED = "EXTRACTED"
    CHUNKING = "CHUNKING"
    CHUNKED = "CHUNKED"
    EMBEDDING = "EMBEDDING"
    EMBEDDED = "EMBEDDED"
    BUILDING_GRAPH = "BUILDING_GRAPH"
    GRAPH_BUILT = "GRAPH_BUILT"
    READY = "READY"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class GraphStatus(StrEnum):
    GRAPH_READY = "GRAPH_READY"
    GRAPH_PARTIAL = "GRAPH_PARTIAL"
    READY_WITHOUT_GRAPH = "READY_WITHOUT_GRAPH"


def assess_graph_status(
    node_count: int,
    relationship_count: int,
    *,
    sections_total: int = 0,
    sections_succeeded: int | None = None,
    batches_failed: int = 0,
    batches_skipped: int = 0,
) -> GraphStatus:
    """Classify usable vector-ready documents without overstating graph quality."""

    if node_count <= 0:
        return GraphStatus.READY_WITHOUT_GRAPH
    coverage_is_partial = (
        sections_total > 0
        and sections_succeeded is not None
        and sections_succeeded < sections_total
    )
    if (
        node_count < 2
        or relationship_count <= 0
        or batches_failed > 0
        or batches_skipped > 0
        or coverage_is_partial
    ):
        return GraphStatus.GRAPH_PARTIAL
    return GraphStatus.GRAPH_READY


class FailureCategory(StrEnum):
    DOCUMENT_ERROR = "DOCUMENT_ERROR"
    CONFIGURATION_ERROR = "CONFIGURATION_ERROR"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    WORKER_ERROR = "WORKER_ERROR"
    TIMEOUT_ERROR = "TIMEOUT_ERROR"
    DATABASE_ERROR = "DATABASE_ERROR"
    UNKNOWN_ERROR = "UNKNOWN_ERROR"


MAX_PROCESSING_ATTEMPTS = 3


def normalize_course_name(value: str) -> str:
    return " ".join(value.strip().casefold().split())


def classify_failure(exc: Exception) -> tuple[FailureCategory, bool, str]:
    message = str(exc).lower()
    if "model" in message and any(
        term in message
        for term in ("does not exist", "model_not_found", "not found", "do not have access")
    ):
        return (
            FailureCategory.CONFIGURATION_ERROR,
            False,
            "The configured AI model is unavailable. Ask an administrator to update the server configuration.",
        )
    if "api_key" in message or "api key" in message or "not configured" in message or "401" in message:
        return FailureCategory.CONFIGURATION_ERROR, False, "The AI provider is not configured. Ask an administrator to update the server configuration."
    if any(term in message for term in ("password", "encrypted", "malformed", "no extractable text", "no readable text")):
        return FailureCategory.DOCUMENT_ERROR, False, "This PDF cannot be processed. Upload a readable, non-encrypted PDF."
    if "not found" in message and ("file" in message or "pdf" in message or "object" in message):
        return FailureCategory.DOCUMENT_ERROR, False, "The source PDF is no longer available. Upload it again."
    if any(term in message for term in ("timeout", "timed out", "429", "rate_limit", "temporarily busy")):
        return FailureCategory.TIMEOUT_ERROR, True, "The AI service is temporarily busy. Retry in a minute."
    if "json_validate_failed" in message:
        return FailureCategory.PROVIDER_ERROR, True, "The AI provider could not structure the document graph. Please retry."
    if any(
        term in message
        for term in (
            "database",
            "postgres",
            "qdrant",
            "neo4j",
            "connection",
            "connecterror",
            "responsehandlingexception",
            "nodename nor servname",
            "name resolution",
            "dns",
            "object storage",
            "bucket",
        )
    ):
        return FailureCategory.DATABASE_ERROR, True, "A storage service is temporarily unavailable. Please retry."
    if any(term in message for term in ("different loop", "worker", "interrupted")):
        return FailureCategory.WORKER_ERROR, True, "Processing was interrupted. Please retry."
    if any(term in message for term in ("provider", "groq", "gemini", "503")):
        return FailureCategory.PROVIDER_ERROR, True, "The AI provider is temporarily unavailable. Please retry."
    return FailureCategory.UNKNOWN_ERROR, False, "Document processing failed. Check the server logs for details."
