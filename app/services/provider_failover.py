import threading
import time
from email.utils import parsedate_to_datetime
import math


def retry_after_seconds(headers) -> float | None:
    """Read standard Retry-After without retaining provider bodies or credentials."""
    value = headers.get("retry-after") if headers else None
    if not value:
        return None
    try:
        seconds = float(value)
    except (ValueError, TypeError):
        try:
            seconds = parsedate_to_datetime(value).timestamp() - time.time()
        except (ValueError, TypeError, OverflowError):
            return None
    return max(0.0, seconds) if math.isfinite(seconds) else None


class ProviderCircuitBreaker:
    """Temporarily bypass providers that have already returned a transient failure."""

    def __init__(self) -> None:
        self._blocked_until: dict[str, float] = {}
        self._lock = threading.Lock()

    def is_available(self, provider: str) -> bool:
        now = time.monotonic()
        with self._lock:
            blocked_until = self._blocked_until.get(provider, 0.0)
            if blocked_until <= now:
                self._blocked_until.pop(provider, None)
                return True
            return False

    def block(self, provider: str, cooldown_seconds: int, error: Exception | None = None) -> None:
        retry_after = getattr(error, "retry_after_seconds", None)
        if retry_after is None:
            retry_after = retry_after_seconds(getattr(getattr(error, "response", None), "headers", None))
        duration = max(cooldown_seconds, retry_after or 0)
        with self._lock:
            self._blocked_until[provider] = max(
                self._blocked_until.get(provider, 0), time.monotonic() + duration
            )

    def clear(self) -> None:
        with self._lock:
            self._blocked_until.clear()


provider_circuit_breaker = ProviderCircuitBreaker()
