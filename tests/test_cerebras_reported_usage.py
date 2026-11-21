from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import httpx

from app.core.exceptions import LLMProviderUnavailableError
from app.services.cerebras_service import CerebrasService
from app.services.provider_metrics import ProviderMetrics


class CerebrasReportedUsageTests(TestCase):
    def observed(self, body, metrics):
        config = SimpleNamespace(cerebras_api_key_value="fixture", cerebras_model="fixture")
        with httpx.Client(base_url="http://fixture", transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=body))) as client, \
                patch("app.services.cerebras_service.settings", config):
            return metrics.observe("cerebras", "graph_extraction", lambda: CerebrasService(client).complete(
                [{"role": "user", "content": "fixture"}]), input_characters=7)

    def test_counts_reported_for_success_and_invalid_completion(self):
        usage = {"prompt_tokens": 4, "completion_tokens": 5, "total_tokens": 10}
        for choices in ([], [{"message": {"content": "OK"}}]):
            metrics = ProviderMetrics()
            if choices:
                self.assertEqual(self.observed({"choices": choices, "usage": usage}, metrics), "OK")
            else:
                with self.assertRaises(LLMProviderUnavailableError):
                    self.observed({"choices": choices, "usage": usage}, metrics)
            totals = metrics.snapshot()["totals"]
            self.assertEqual([totals[k] for k in ("reported_input_tokens", "reported_output_tokens",
                                                   "reported_total_tokens")], [4, 5, 10])

    def test_missing_or_invalid_fields_remain_unknown(self):
        for usage in (None, {}, {"prompt_tokens": True, "completion_tokens": 5, "total_tokens": 6}):
            metrics = ProviderMetrics()
            self.observed({"usage": usage, "choices": [{"message": {"content": "OK"}}]}, metrics)
            self.assertEqual(metrics.snapshot()["totals"]["unreported_usage_attempts"], 1)
