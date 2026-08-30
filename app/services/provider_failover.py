import threading
import time


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

    def block(self, provider: str, cooldown_seconds: int) -> None:
        with self._lock:
            self._blocked_until[provider] = time.monotonic() + cooldown_seconds

    def clear(self) -> None:
        with self._lock:
            self._blocked_until.clear()


provider_circuit_breaker = ProviderCircuitBreaker()
