class LLMConfigurationError(RuntimeError):
    """Raised when the selected LLM provider is missing required credentials."""


class GraphStructureError(RuntimeError):
    """Raised when a provider response cannot be validated as a concept graph."""
