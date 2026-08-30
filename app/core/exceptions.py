class LLMConfigurationError(RuntimeError):
    """Raised when the selected LLM provider is missing required credentials."""


class GraphStructureError(RuntimeError):
    """Raised when a provider response cannot be validated as a concept graph."""


class LLMProviderRateLimitError(RuntimeError):
    """Raised when an LLM provider rejects a request because its quota is exhausted."""


class LLMProviderUnavailableError(RuntimeError):
    """Raised when an LLM provider times out or is temporarily unavailable."""


class LLMProviderRequestError(RuntimeError):
    """Raised when an LLM provider rejects an otherwise valid request."""
