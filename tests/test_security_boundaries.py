import unittest
from unittest.mock import AsyncMock, patch

from starlette.requests import Request
from starlette.responses import JSONResponse

from app.core.config import Settings
from app.core.security import DemoProtectionMiddleware
from app.services.security_service import DemoAccessService, RateLimitService


class SecurityBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = Settings(_env_file=None, DEMO_ACCESS_TOKEN="test-reviewer-access-code-12345678")
        self.access = DemoAccessService(self.config)

    async def test_cookie_upload_requires_csrf_header_but_bearer_still_works(self):
        cookie = (b"cookie", f"{self.config.auth_cookie_name}={self.access.issue_cookie()}".encode())
        cases = [
            ([cookie], 403),
            ([cookie, (b"x-conceptgraph-request", b"1")], 200),
            ([(b"authorization", f"Bearer {self.config.demo_access_token_value}".encode())], 200),
            ([(b"x-conceptgraph-request", b"1")], 401),
        ]
        with patch("app.core.security.demo_access_service", self.access), \
             patch("app.core.security.rate_limit_service", RateLimitService()):
            for headers, expected in cases:
                request = Request({"type": "http", "method": "POST", "path": "/api/v1/ingest/upload",
                    "headers": headers, "query_string": b"", "scheme": "https", "server": ("api.example", 443)})
                next_handler = AsyncMock(return_value=JSONResponse({"ok": True}))
                response = await DemoProtectionMiddleware(AsyncMock()).dispatch(request, next_handler)
                self.assertEqual(response.status_code, expected)
                if expected == 200:
                    self.assertEqual(response.headers["Cache-Control"], "no-store")
                else:
                    next_handler.assert_not_awaited()

    def test_unicode_credentials_are_rejected_without_server_error(self):
        self.assertFalse(self.access.verify_access_token("密码"))
        self.assertFalse(self.access.verify_cookie("1000.密码", now=1001))
        self.assertFalse(self.access.verify_cookie("١٠٠٠.bad", now=1001))

    async def test_limiter_expires_old_clients_and_bounds_memory(self):
        limiter = RateLimitService()
        await limiter.check("old-client", 2, now=60)
        await limiter.check("new-client", 2, now=120)
        self.assertNotIn("old-client", limiter._counts)
        limiter._counts = {str(i): (2, 1) for i in range(10_000)}
        self.assertFalse((await limiter.check("overflow", 2, now=120)).allowed)
        self.assertEqual(len(limiter._counts), 10_000)
