from __future__ import annotations

import math
import threading
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any, TypeVar

from pydantic import BaseModel

from app.core.exceptions import (
    GraphStructureError,
    LLMConfigurationError,
    LLMProviderRateLimitError,
    LLMProviderRequestError,
    LLMProviderUnavailableError,
)


T = TypeVar("T")
_reported_usage: ContextVar[list[tuple[int, int, int]] | None] = ContextVar("reported_provider_usage", default=None)
_reported_search_units: ContextVar[list[float] | None] = ContextVar("reported_provider_search_units", default=None)


def record_provider_usage(input_tokens, output_tokens, total_tokens) -> None:
    """Capture only sanitized counts inside the current measured provider call."""
    collector = _reported_usage.get()
    values = (input_tokens, output_tokens, total_tokens)
    if collector is not None and all(type(value) is int and value >= 0 for value in values):
        collector.append(values)


def record_provider_search_units(search_units) -> None:
    """Capture reported rerank units separately from token counts and cost."""
    collector = _reported_search_units.get()
    if collector is None or type(search_units) not in (int, float):
        return
    try:
        value = float(search_units)
    except (OverflowError, ValueError):
        return
    if math.isfinite(value) and value >= 0:
        collector.append(value)


@dataclass(frozen=True, slots=True)
class ProviderCallMetric:
    provider: str
    operation: str
    outcome: str
    latency_ms: int
    failover: bool
    estimated_input_tokens: int
    estimated_output_tokens: int
    recorded_at: str
    failure_reason: str | None = None
    repair: bool = False
    reported_input_tokens: int | None = None
    reported_output_tokens: int | None = None
    reported_total_tokens: int | None = None
    reported_search_units: float | None = None


class ProviderMetrics:
    """Bounded, process-local LLM telemetry with no prompts, answers, or keys."""

    def __init__(self, max_events: int = 2_000) -> None:
        self._max_events = max_events
        self._lock = threading.Lock()
        self._events: deque[ProviderCallMetric] = deque(maxlen=max_events)
        self._evidence_fallbacks = 0
        self._started_at = self._now()

    def observe(
        self,
        provider: str,
        operation: str,
        func: Callable[[], T],
        *,
        input_characters: int,
        failover: bool = False,
        repair: bool = False,
    ) -> T:
        usage: list[tuple[int, int, int]] = []
        token = _reported_usage.set(usage)
        try:
            return self._observe(provider, operation, func, input_characters=input_characters,
                failover=failover, repair=repair, usage=usage)
        finally:
            _reported_usage.reset(token)

    async def observe_async(
        self,
        provider: str,
        operation: str,
        func: Callable[[], Awaitable[T]],
        *,
        input_characters: int,
        failover: bool = False,
        repair: bool = False,
        output_characters: int | None = None,
    ) -> T:
        """Measure one asynchronous provider request and collect its reported metadata."""
        usage: list[tuple[int, int, int]] = []
        search_units: list[float] = []
        usage_token = _reported_usage.set(usage)
        units_token = _reported_search_units.set(search_units)
        started = time.perf_counter()
        try:
            result = await func()
        except Exception as exc:
            self._record(
                provider=provider,
                operation=operation,
                outcome=self._classify_exception(exc),
                latency_ms=self._elapsed_ms(started),
                failover=failover,
                input_characters=input_characters,
                output_characters=0,
                failure_reason=exc.reason.value if isinstance(exc, GraphStructureError) else None,
                repair=repair,
                usage=usage,
                search_units=search_units,
            )
            raise
        else:
            self._record(
                provider=provider,
                operation=operation,
                outcome="success",
                latency_ms=self._elapsed_ms(started),
                failover=failover,
                input_characters=input_characters,
                output_characters=(self._output_size(result) if output_characters is None else output_characters),
                repair=repair,
                usage=usage,
                search_units=search_units,
            )
            return result
        finally:
            _reported_search_units.reset(units_token)
            _reported_usage.reset(usage_token)

    def _observe(self, provider, operation, func, *, input_characters, failover, repair, usage):
        started = time.perf_counter()
        try:
            result = func()
        except Exception as exc:
            self._record(
                provider=provider,
                operation=operation,
                outcome=self._classify_exception(exc),
                latency_ms=self._elapsed_ms(started),
                failover=failover,
                input_characters=input_characters,
                output_characters=0,
                failure_reason=exc.reason.value if isinstance(exc, GraphStructureError) else None,
                repair=repair,
                usage=usage,
            )
            raise

        self._record(
            provider=provider,
            operation=operation,
            outcome="success",
            latency_ms=self._elapsed_ms(started),
            failover=failover,
            input_characters=input_characters,
            output_characters=self._output_size(result),
            repair=repair,
            usage=usage,
        )
        return result

    def record_evidence_fallback(self) -> None:
        with self._lock:
            self._evidence_fallbacks += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            events = list(self._events)
            evidence_fallbacks = self._evidence_fallbacks
            started_at = self._started_at

        successful = [event for event in events if event.outcome == "success"]
        latencies = sorted(event.latency_ms for event in events)
        return {
            "scope": "current_api_process",
            "started_at": started_at,
            "generated_at": self._now(),
            "retained_event_limit": self._max_events,
            "totals": {
                "provider_attempts": len(events),
                "successful_attempts": len(successful),
                "failed_attempts": len(events) - len(successful),
                "failover_attempts": sum(event.failover for event in events),
                "repair_attempts": sum(event.repair for event in events),
                "rate_limit_errors": sum(event.outcome == "rate_limited" for event in events),
                "evidence_fallbacks": evidence_fallbacks,
                "estimated_input_tokens": sum(event.estimated_input_tokens for event in events),
                "estimated_output_tokens": sum(event.estimated_output_tokens for event in events),
                "reported_usage_attempts": sum(event.reported_total_tokens is not None for event in events),
                "unreported_usage_attempts": sum(event.reported_total_tokens is None for event in events),
                "reported_input_tokens": sum(event.reported_input_tokens or 0 for event in events),
                "reported_output_tokens": sum(event.reported_output_tokens or 0 for event in events),
                "reported_total_tokens": sum(event.reported_total_tokens or 0 for event in events),
                "reported_search_unit_attempts": sum(event.reported_search_units is not None for event in events),
                "reported_search_units": sum(event.reported_search_units or 0 for event in events),
                "average_latency_ms": self._average_latency(events),
                "p95_latency_ms": self._percentile(latencies, 0.95),
            },
            "by_provider": self._grouped(events, "provider"),
            "by_operation": self._grouped(events, "operation"),
            "recent_events": [asdict(event) for event in events[-20:]],
            "privacy_note": (
                "Only aggregate call metadata is retained in memory. Prompts, answers, "
                "document identifiers, and credentials are never stored in these metrics."
            ),
            "accuracy_note": (
                "estimated_* token counts are character-based estimates. reported_* counts cover only "
                "responses whose adapters capture usage (Groq/Gemini); missing usage is unknown, "
                "not zero. No cost or complete provider-quota accounting is implied. Provider billing "
                "dashboards remain authoritative. Gemini reported output counts response candidates; "
                "its reported total can additionally include thinking/tool-use tokens. Cohere "
                "rerank search units are reported separately and are not token counts or cost. "
                "Metrics reset whenever the API process restarts."
            ),
        }

    def clear(self) -> None:
        with self._lock:
            self._events.clear()
            self._evidence_fallbacks = 0
            self._started_at = self._now()

    def _record(
        self,
        *,
        provider: str,
        operation: str,
        outcome: str,
        latency_ms: int,
        failover: bool,
        input_characters: int,
        output_characters: int,
        failure_reason: str | None = None,
        repair: bool = False,
        usage: list[tuple[int, int, int]] | None = None,
        search_units: list[float] | None = None,
    ) -> None:
        event = ProviderCallMetric(
            provider=provider,
            operation=operation,
            outcome=outcome,
            latency_ms=max(0, latency_ms),
            failover=failover,
            estimated_input_tokens=self._estimate_tokens(input_characters),
            estimated_output_tokens=self._estimate_tokens(output_characters),
            recorded_at=self._now(),
            failure_reason=failure_reason,
            repair=repair,
            reported_input_tokens=sum(row[0] for row in usage) if usage else None,
            reported_output_tokens=sum(row[1] for row in usage) if usage else None,
            reported_total_tokens=sum(row[2] for row in usage) if usage else None,
            reported_search_units=sum(search_units) if search_units else None,
        )
        with self._lock:
            self._events.append(event)

    @staticmethod
    def _classify_exception(exc: Exception) -> str:
        if isinstance(exc, LLMProviderRateLimitError) or type(exc).__name__ == "RateLimitError":
            return "rate_limited"
        if isinstance(exc, LLMConfigurationError) or type(exc).__name__ in {
            "AuthenticationError",
            "PermissionDeniedError",
        }:
            return "configuration_error"
        if isinstance(exc, GraphStructureError) or isinstance(exc, ValueError):
            return "invalid_response"
        if isinstance(exc, LLMProviderUnavailableError) or type(exc).__name__ in {
            "APIConnectionError",
            "APITimeoutError",
            "InternalServerError",
            "TimeoutError",
        }:
            return "unavailable"
        if isinstance(exc, LLMProviderRequestError) or type(exc).__name__ == "BadRequestError":
            return "request_error"
        return "unexpected_error"

    @staticmethod
    def _output_size(result: Any) -> int:
        if isinstance(result, str):
            return len(result)
        if isinstance(result, BaseModel):
            return len(result.model_dump_json())
        if isinstance(result, (list, tuple)):
            return sum(ProviderMetrics._output_size(item) for item in result)
        return len(str(result))

    @staticmethod
    def _estimate_tokens(characters: int) -> int:
        return max(0, math.ceil(max(0, characters) / 4))

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        return round((time.perf_counter() - started) * 1_000)

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    @staticmethod
    def _average_latency(events: list[ProviderCallMetric]) -> int:
        if not events:
            return 0
        return round(sum(event.latency_ms for event in events) / len(events))

    @staticmethod
    def _percentile(values: list[int], percentile: float) -> int:
        if not values:
            return 0
        index = max(0, math.ceil(len(values) * percentile) - 1)
        return values[index]

    @classmethod
    def _grouped(
        cls,
        events: list[ProviderCallMetric],
        attribute: str,
    ) -> list[dict[str, Any]]:
        grouped: dict[str, list[ProviderCallMetric]] = defaultdict(list)
        for event in events:
            grouped[str(getattr(event, attribute))].append(event)
        return [
            {
                "name": name,
                "attempts": len(group),
                "successful_attempts": sum(event.outcome == "success" for event in group),
                "failed_attempts": sum(event.outcome != "success" for event in group),
                "failover_attempts": sum(event.failover for event in group),
                "rate_limit_errors": sum(event.outcome == "rate_limited" for event in group),
                "estimated_input_tokens": sum(event.estimated_input_tokens for event in group),
                "estimated_output_tokens": sum(event.estimated_output_tokens for event in group),
                "reported_usage_attempts": sum(event.reported_total_tokens is not None for event in group),
                "reported_input_tokens": sum(event.reported_input_tokens or 0 for event in group),
                "reported_output_tokens": sum(event.reported_output_tokens or 0 for event in group),
                "reported_total_tokens": sum(event.reported_total_tokens or 0 for event in group),
                "reported_search_unit_attempts": sum(event.reported_search_units is not None for event in group),
                "reported_search_units": sum(event.reported_search_units or 0 for event in group),
                "average_latency_ms": cls._average_latency(group),
            }
            for name, group in sorted(grouped.items())
        ]


provider_metrics = ProviderMetrics()
