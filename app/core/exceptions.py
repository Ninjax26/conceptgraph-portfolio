class LLMConfigurationError(RuntimeError):
    """Raised when the selected LLM provider is missing required credentials."""


class GraphStructureError(RuntimeError):
    """Raised when a provider response cannot be validated as a concept graph."""


class LLMProviderRateLimitError(RuntimeError):
    """Raised when an LLM provider rejects a request because its quota is exhausted."""

    def __init__(self, message: str, *, retry_after_seconds: float | None = None):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class LLMProviderUnavailableError(RuntimeError):
    """Raised when an LLM provider times out or is temporarily unavailable."""


class LLMProviderRequestError(RuntimeError):
    """Raised when an LLM provider rejects an otherwise valid request."""
