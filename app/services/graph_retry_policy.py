"""Bounded, source-free retry metadata; scheduling is separate from lease fencing."""

import math
from datetime import datetime, timedelta

from app.core.config import settings
from app.services.provider_failover import retry_after_seconds

MAX_GRAPH_RETRY_DELAY = 7 * 24 * 60 * 60


class GraphRetryNotDue(RuntimeError):
    def __init__(self, seconds: int):
        super().__init__("Graph processing is waiting for provider capacity. Retry after the indicated delay.")
        self.retry_after_seconds = seconds


def quota_retry_delay(error: Exception) -> float:
    value = getattr(error, "retry_after_seconds", None)
    if value is None:
        value = retry_after_seconds(getattr(getattr(error, "response", None), "headers", None))
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError):
        seconds = settings.llm_failover_cooldown_seconds
    if not math.isfinite(seconds) or seconds < 0:
        seconds = settings.llm_failover_cooldown_seconds
    if not getattr(error, "application_budget", False):
        # Vendor denials also open the existing circuit. Do not offer an earlier
        # retry than that circuit's minimum cooldown. Local holdback does not.
        seconds = max(seconds, settings.llm_failover_cooldown_seconds)
    return min(MAX_GRAPH_RETRY_DELAY, max(1.0, seconds))


def retry_not_before(delay, now: datetime) -> str | None:
    if type(delay) not in {int, float} or not math.isfinite(delay) or delay < 1:
        return None
    return (now + timedelta(seconds=min(delay, MAX_GRAPH_RETRY_DELAY))).isoformat()


def retry_wait_seconds(result, now: datetime) -> int:
    if not isinstance(result, dict):
        return 0
    value = result.get("graph_retry_not_before")
    if not isinstance(value, str):
        return 0
    try:
        deadline = datetime.fromisoformat(value)
    except ValueError:
        return 0
    # Legacy/malformed metadata is not a new permanent retry prohibition.
    if deadline.tzinfo is None:
        return 0
    return max(0, math.ceil((deadline - now).total_seconds()))
