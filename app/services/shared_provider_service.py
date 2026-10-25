"""PostgreSQL-backed request budgets shared by API and ingestion processes."""

import math
from contextvars import ContextVar

from sqlalchemy import text

from app.core.config import settings
from app.core.blocking import run_blocking_safely
from app.core.database import AsyncSessionLocal
from app.core.exceptions import LLMProviderRateLimitError, LLMProviderUnavailableError, SharedProviderBudgetExceeded
from app.services.provider_metrics import provider_metrics
from app.services.provider_budget_policy import BudgetUsage, admission_denial, estimated_reservation

provider_call_counts: ContextVar[dict[str, int] | None] = ContextVar("provider_call_counts", default=None)


async def reserve_provider_request(provider: str, *, operation: str = "interactive", estimated_tokens: int = 0):
    if type(estimated_tokens) is not int or estimated_tokens < 0:
        raise ValueError("Estimated token reservation must be a nonnegative integer.")
    if not settings.shared_provider_limits_enabled:
        if settings.processing_role in {"api", "worker"}:
            raise LLMProviderUnavailableError("Shared provider budgets must be enabled for split-process operation.")
        return
    try:
        async with AsyncSessionLocal() as session:
            async with session.begin():
                await session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                                      {"key": f"conceptgraph:provider:{settings.shared_provider_scope}:{provider}"})
                epoch = float((await session.execute(text("SELECT extract(epoch FROM clock_timestamp())"))).scalar_one())
                minute, day = math.floor(epoch / 60), math.floor(epoch / 86400)
                params = {"scope": settings.shared_provider_scope, "provider": provider, "minute": minute, "day": day}
                await session.execute(text("INSERT INTO provider_budgets (scope, provider, minute_window, day_window, minute_requests, day_requests) "
                                           "VALUES (:scope, :provider, :minute, :day, 0, 0) ON CONFLICT DO NOTHING"), params)
                row = (await session.execute(text("SELECT minute_window, day_window, minute_requests, day_requests, minute_estimated_tokens, day_estimated_tokens FROM provider_budgets "
                                                  "WHERE scope=:scope AND provider=:provider FOR UPDATE"), params)).one()
                minute_count = row.minute_requests if row.minute_window == minute else 0
                day_count = row.day_requests if row.day_window == day else 0
                minute_tokens = row.minute_estimated_tokens if row.minute_window == minute else 0
                day_tokens = row.day_estimated_tokens if row.day_window == day else 0
                denial = admission_denial(settings, BudgetUsage(minute_count, day_count, minute_tokens, day_tokens),
                                          operation=operation, estimated_tokens=estimated_tokens, epoch=epoch)
                if denial is not None:
                    reason, retry = denial
                    raise SharedProviderBudgetExceeded(f"Application-wide provider {reason} budget reached.", retry_after_seconds=retry)
                await session.execute(text("UPDATE provider_budgets SET minute_window=:minute, day_window=:day, "
                                           "minute_requests=:minute_count, day_requests=:day_count, "
                                           "minute_estimated_tokens=:minute_tokens, day_estimated_tokens=:day_tokens "
                                           "WHERE scope=:scope AND provider=:provider"),
                                      {**params, "minute_count": minute_count + 1, "day_count": day_count + 1,
                                       "minute_tokens": minute_tokens + estimated_tokens, "day_tokens": day_tokens + estimated_tokens})
    except LLMProviderRateLimitError:
        raise
    except Exception as exc:
        raise LLMProviderUnavailableError("Shared provider budget storage is unavailable; no provider request was sent.") from exc


async def observe_provider_call(provider, operation, generate, *, input_characters, failover=False, repair=False):
    await reserve_provider_request(provider, operation=operation, estimated_tokens=estimated_reservation(input_characters, operation))
    counters = provider_call_counts.get()
    if counters is not None:
        counters[operation] = counters.get(operation, 0) + 1
    return await run_blocking_safely(provider_metrics.observe, provider, operation, generate,
                                   input_characters=input_characters, failover=failover, repair=repair)


async def shared_provider_status():
    status = {"enabled": settings.shared_provider_limits_enabled,
              "requests_per_minute": settings.provider_requests_per_minute,
              "requests_per_day": settings.provider_requests_per_day,
              "estimated_tokens_per_minute": settings.provider_estimated_tokens_per_minute,
              "estimated_tokens_per_day": settings.provider_estimated_tokens_per_day,
              "interactive_reserves": {
                  "requests_per_minute": settings.provider_interactive_request_reserve_per_minute,
                  "requests_per_day": settings.provider_interactive_request_reserve_per_day,
                  "estimated_tokens_per_minute": settings.provider_interactive_token_reserve_per_minute,
                  "estimated_tokens_per_day": settings.provider_interactive_token_reserve_per_day,
              },
              "usage": [], "note": "Attempt reservations shared by database/scope. UTC windows; failures and cancellations retain reservations. Tokens estimate characters/4 plus an operation output allowance, not vendor billing or a hard token quota. Zero token limits/reserves disable enforcement. Earlier requests and older processes do not supply token reservations. Cohere rerank attempts, Qdrant-hosted document upserts and query embeddings are reserved before their requests; Cohere-reported rerank search units are recorded separately from token counts. Qdrant-hosted embedding usage is not captured."}
    if not status["enabled"]:
        return status
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(text("SELECT provider, CASE WHEN minute_window=floor(extract(epoch FROM now())/60) "
                                           "THEN minute_requests ELSE 0 END AS minute_requests, "
                                           "CASE WHEN day_window=floor(extract(epoch FROM now())/86400) "
                                           "THEN day_requests ELSE 0 END AS day_requests, "
                                           "CASE WHEN minute_window=floor(extract(epoch FROM now())/60) THEN minute_estimated_tokens ELSE 0 END AS minute_estimated_tokens, "
                                           "CASE WHEN day_window=floor(extract(epoch FROM now())/86400) THEN day_estimated_tokens ELSE 0 END AS day_estimated_tokens "
                                           "FROM provider_budgets WHERE scope=:scope"),
                                      {"scope": settings.shared_provider_scope})).mappings().all()
    status["usage"] = [dict(row) for row in rows]
    return status
