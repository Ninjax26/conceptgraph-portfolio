"""The removed practice feature must not leave an API or frontend entry point."""

from pathlib import Path
import unittest
from unittest.mock import patch

import httpx

from app.main import app
from app.core.config import Settings
from app.services.security_service import DemoAccessService, RateLimitService


class RemovedExamTests(unittest.IsolatedAsyncioTestCase):
    async def test_exam_endpoint_is_gone_but_main_query_remains_protected(self):
        cfg = Settings(_env_file=None, DEMO_ACCESS_TOKEN="fixture-reviewer-code-12345678")
        with patch("app.core.security.settings", cfg), \
             patch("app.core.security.demo_access_service", DemoAccessService(cfg)), \
             patch("app.core.security.rate_limit_service", RateLimitService()):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                # Unknown API paths still pass the unchanged authentication boundary.
                self.assertEqual((await client.post("/api/v1/exam/generate", json={},
                    headers={"Authorization":f"Bearer {cfg.demo_access_token_value}"})).status_code, 404)
                self.assertEqual((await client.post("/api/v1/query", json={})).status_code, 401)
        paths = app.openapi()["paths"]
        self.assertNotIn("/api/v1/exam/generate", paths)
        self.assertIn("/api/v1/query", paths)

    async def test_dashboard_and_client_have_no_exam_entry_point(self):
        root = Path(__file__).resolve().parents[1]
        self.assertNotIn("ExamPanel", (root / "src/pages/Dashboard.tsx").read_text())
        self.assertNotIn("generateExam", (root / "src/services/api.ts").read_text())
        self.assertFalse((root / "app/services/exam_service.py").exists())
        self.assertFalse((root / "src/components/ExamPanel.tsx").exists())
