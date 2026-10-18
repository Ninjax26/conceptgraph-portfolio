"""Mock REST responses only; no hosted provider calls or credential reads."""

from types import SimpleNamespace
import unittest
from unittest.mock import patch
import httpx

from app.core.exceptions import LLMProviderTruncatedResponseError, LLMProviderUnavailableError
from app.services.gemini_service import GeminiService
from app.services.provider_metrics import ProviderMetrics


class GeminiReportedUsageTests(unittest.TestCase):
    def observed(self, body, metrics, **kwargs):
        config = SimpleNamespace(gemini_api_key="fixture", gemini_model="fixture", provider_timeout_seconds=1)
        with httpx.Client(base_url=GeminiService.BASE_URL, transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=body))) as client, \
             patch("app.services.gemini_service.settings", config):
            return metrics.observe("gemini", "fixture", lambda: GeminiService(client).generate(
                ["private fixture text"], **kwargs), input_characters=20)

    def body(self, usage=None, *, text="Supported answer", reason="STOP"):
        body = {"candidates":[{"finishReason":reason, "content":{"parts":[{"text":text}]}}]}
        if usage is not None:
            body["usageMetadata"] = usage
        return body

    def test_thinking_total_is_preserved_not_recomputed(self):
        metrics = ProviderMetrics()
        usage = dict(promptTokenCount=10, candidatesTokenCount=20, thoughtsTokenCount=5, totalTokenCount=35)
        self.assertEqual(self.observed(self.body(usage), metrics), "Supported answer")
        totals = metrics.snapshot()["totals"]
        self.assertEqual(totals["reported_input_tokens"], 10)
        self.assertEqual(totals["reported_output_tokens"], 20)
        self.assertEqual(totals["reported_total_tokens"], 35)
        self.assertNotIn("private fixture text", str(metrics.snapshot()))

    def test_missing_metadata_does_not_invent_counts(self):
        metrics = ProviderMetrics()
        self.observed(self.body(), metrics)
        self.assertEqual(metrics.snapshot()["totals"]["unreported_usage_attempts"], 1)

    def test_invalid_metadata_does_not_break_answer_or_record_usage(self):
        for usage in ("invalid", {}, dict(promptTokenCount=True, candidatesTokenCount=2, totalTokenCount=3),
                      dict(promptTokenCount=1, candidatesTokenCount=-1, totalTokenCount=0)):
            metrics = ProviderMetrics()
            with self.subTest(usage=usage):
                self.assertEqual(self.observed(self.body(usage), metrics), "Supported answer")
                self.assertEqual(metrics.snapshot()["totals"]["reported_usage_attempts"], 0)

    def test_truncated_or_empty_completion_keeps_reported_usage(self):
        usage = dict(promptTokenCount=10, candidatesTokenCount=20, totalTokenCount=30)
        for text, reason, error in (("partial", "MAX_TOKENS", LLMProviderTruncatedResponseError),
                                    ("", "STOP", LLMProviderUnavailableError)):
            metrics = ProviderMetrics()
            with self.subTest(reason=reason), self.assertRaises(error):
                self.observed(self.body(usage, text=text, reason=reason), metrics, require_complete=True)
            self.assertEqual(metrics.snapshot()["totals"]["reported_total_tokens"], 30)
