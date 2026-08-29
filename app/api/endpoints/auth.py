from fastapi import APIRouter, HTTPException, Request, Response, status

from app.core.config import settings
from app.schemas.auth import AccessCodeRequest, AuthSessionResponse
from app.services.security_service import demo_access_service, rate_limit_service

router = APIRouter(prefix="/api/v1/auth", tags=["authentication"])


@router.post("/session", response_model=AuthSessionResponse)
async def create_session(
    payload: AccessCodeRequest,
    request: Request,
    response: Response,
) -> AuthSessionResponse:
    response.headers["Cache-Control"] = "no-store"
    if not demo_access_service.enabled:
        return AuthSessionResponse(enabled=False, authenticated=True)

    client_host = request.client.host if request.client else "unknown"
    client_key = demo_access_service.fingerprint(client_host)
    rate = await rate_limit_service.check(
        f"login:{client_key}",
        settings.rate_limit_login_per_minute,
    )

    if not rate.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many access attempts. Please wait before trying again.",
            headers={"Retry-After": str(rate.retry_after)},
        )
    if not demo_access_service.verify_access_token(payload.access_code):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="The access code is invalid.",
        )

    response.set_cookie(
        key=settings.auth_cookie_name,
        value=demo_access_service.issue_cookie(),
        max_age=settings.auth_session_ttl_seconds,
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite=settings.auth_cookie_samesite,
        path="/",
    )
    return AuthSessionResponse(
        enabled=True,
        authenticated=True,
        expires_in_seconds=settings.auth_session_ttl_seconds,
    )


@router.get("/session", response_model=AuthSessionResponse)
async def get_session(request: Request, response: Response) -> AuthSessionResponse:
    response.headers["Cache-Control"] = "no-store"
    if not demo_access_service.enabled:
        return AuthSessionResponse(
            enabled=False,
            authenticated=True,
            expires_in_seconds=None,
        )

    expires_in = demo_access_service.session_expires_in(
        request.cookies.get(settings.auth_cookie_name)
    )
    return AuthSessionResponse(
        enabled=True,
        authenticated=expires_in is not None,
        expires_in_seconds=expires_in,
    )


@router.delete("/session", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.delete_cookie(
        key=settings.auth_cookie_name,
        path="/",
        secure=settings.auth_cookie_secure,
        httponly=True,
        samesite=settings.auth_cookie_samesite,
    )
