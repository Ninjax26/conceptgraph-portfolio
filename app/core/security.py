from __future__ import annotations

from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from app.core.config import settings
from app.services.security_service import demo_access_service, rate_limit_service

PUBLIC_PATHS = {"/api/v1/health", "/api/v1/ready"}
PUBLIC_METHOD_PATHS = {("POST", "/api/v1/auth/session")}
EXPENSIVE_PATHS = {
    "/api/v1/query",
    "/api/v1/exam/generate",
    "/api/v1/ingest/upload",
}


class DemoProtectionMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if not demo_access_service.enabled or not _is_protected(request):
            return await call_next(request)

        credential = _request_credential(request)
        if credential is None:
            return JSONResponse(
                status_code=401,
                content={"detail": "Authentication is required."},
                headers={"WWW-Authenticate": "Bearer"},
            )

        rate_limit = (
            settings.rate_limit_expensive_per_minute
            if _is_expensive(request)
            else settings.rate_limit_requests_per_minute
        )
        rate_scope = "expensive" if _is_expensive(request) else "standard"
        principal = settings.demo_access_token_value or credential
        fingerprint = demo_access_service.fingerprint(principal)
        result = await rate_limit_service.check(
            f"{rate_scope}:{fingerprint}",
            rate_limit,
        )

        rate_headers = {
            "X-RateLimit-Limit": str(result.limit),
            "X-RateLimit-Remaining": str(result.remaining),
        }
        if not result.allowed:
            return JSONResponse(
                status_code=429,
                content={"detail": "Request limit reached. Please try again shortly."},
                headers={**rate_headers, "Retry-After": str(result.retry_after)},
            )

        response = await call_next(request)
        response.headers.update(rate_headers)
        return response


def _is_protected(request: Request) -> bool:
    path = request.url.path.rstrip("/") or "/"
    if request.method == "OPTIONS":
        return False
    if path in PUBLIC_PATHS or (request.method, path) in PUBLIC_METHOD_PATHS:
        return False
    return path.startswith("/api/v1") or path in {"/docs", "/redoc", "/openapi.json"}


def _is_expensive(request: Request) -> bool:
    path = request.url.path.rstrip("/")
    return request.method == "POST" and (
        path in EXPENSIVE_PATHS or path.endswith("/retry")
    )


def _request_credential(request: Request) -> str | None:
    authorization = request.headers.get("Authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.casefold() == "bearer" and demo_access_service.verify_access_token(token):
        return token

    cookie = request.cookies.get(settings.auth_cookie_name)
    if demo_access_service.verify_cookie(cookie):
        return cookie
    return None
