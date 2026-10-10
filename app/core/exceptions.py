from enum import StrEnum


class LLMConfigurationError(RuntimeError):
    """Raised when the selected LLM provider is missing required credentials."""


class LLMProviderTruncatedResponseError(RuntimeError):
    """Raised when a structured completion exhausted its output token budget."""


class GraphFailureReason(StrEnum):
    MALFORMED_JSON = "malformed_json"
    INVALID_SCHEMA = "invalid_schema"
    INVALID_ENDPOINTS = "invalid_endpoints"
    DUPLICATE_IDS = "duplicate_node_ids"
    UNSUPPORTED_RELATIONSHIP = "unsupported_relationship_type"
    UNKNOWN_SOURCE = "unknown_source"
    BATCH_LIMIT = "batch_limit"
    TRUNCATED_RESPONSE = "truncated_response"


class GraphStructureError(RuntimeError):
    """Raised when a provider response cannot be validated as a concept graph."""

    def __init__(self, message: str, *, reason: GraphFailureReason = GraphFailureReason.INVALID_SCHEMA):
        super().__init__(message)
        self.reason = GraphFailureReason(reason)


class LLMProviderRateLimitError(RuntimeError):
    """Raised when an LLM provider rejects a request because its quota is exhausted."""

    def __init__(self, message: str, *, retry_after_seconds: float | None = None):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class LLMProviderUnavailableError(RuntimeError):
    """Raised when an LLM provider times out or is temporarily unavailable."""


class SharedProviderBudgetExceeded(LLMProviderRateLimitError):
    """The application's shared budget is full, not necessarily vendor quota."""
    application_budget = True


class LLMProviderRequestError(RuntimeError):
    """Raised when an LLM provider rejects an otherwise valid request."""
