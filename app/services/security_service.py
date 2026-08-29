from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import hmac
import time

from app.core.config import Settings, settings


@dataclass(frozen=True, slots=True)
class RateLimitResult:
    allowed: bool
    limit: int
    remaining: int
    retry_after: int


class DemoAccessService:
    def __init__(self, config: Settings = settings) -> None:
        self.config = config

    @property
    def enabled(self) -> bool:
        return self.config.demo_access_token_value is not None

    def verify_access_token(self, candidate: str | None) -> bool:
        expected = self.config.demo_access_token_value
        if expected is None or candidate is None:
            return False
        return hmac.compare_digest(candidate, expected)

    def issue_cookie(self, *, now: int | None = None) -> str:
        timestamp = str(now if now is not None else int(time.time()))
        return f"{timestamp}.{self._sign(timestamp)}"

    def verify_cookie(self, cookie_value: str | None, *, now: int | None = None) -> bool:
        return self.session_expires_in(cookie_value, now=now) is not None

    def session_expires_in(
        self,
        cookie_value: str | None,
        *,
        now: int | None = None,
    ) -> int | None:
        if not cookie_value or not self.enabled:
            return None
        try:
            timestamp, signature = cookie_value.split(".", maxsplit=1)
            issued_at = int(timestamp)
        except (TypeError, ValueError):
            return None

        current_time = now if now is not None else int(time.time())
        if issued_at > current_time + 30:
            return None
        age = current_time - issued_at
        if age > self.config.auth_session_ttl_seconds:
            return None
        if not hmac.compare_digest(signature, self._sign(timestamp)):
            return None
        return max(0, self.config.auth_session_ttl_seconds - age)

    @staticmethod
    def fingerprint(credential: str) -> str:
        return hashlib.sha256(credential.encode("utf-8")).hexdigest()[:24]

    def _sign(self, timestamp: str) -> str:
        secret = self.config.demo_access_token_value
        if secret is None:
            raise RuntimeError("Demo access protection is not configured.")
        return hmac.new(
            secret.encode("utf-8"),
            timestamp.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()


class RateLimitService:
    """Fixed-window limiter for the portfolio edition's single API process.

    Counters intentionally reset when the process restarts. Durable document
    limits remain PostgreSQL-backed; this limiter protects request bursts.
    """

    def __init__(self) -> None:
        self._counts: dict[str, tuple[int, int]] = {}
        self._lock = asyncio.Lock()

    async def check(
        self,
        key: str,
        limit: int,
        *,
        now: int | None = None,
    ) -> RateLimitResult:
        current_time = now if now is not None else int(time.time())
        window = current_time // 60
        async with self._lock:
            stored_window, stored_count = self._counts.get(key, (window, 0))
            count = stored_count + 1 if stored_window == window else 1
            self._counts[key] = (window, count)
        return RateLimitResult(
            allowed=count <= limit,
            limit=limit,
            remaining=max(0, limit - count),
            retry_after=max(1, 60 - (current_time % 60)),
        )

    async def close(self) -> None:
        async with self._lock:
            self._counts.clear()


demo_access_service = DemoAccessService()
rate_limit_service = RateLimitService()
