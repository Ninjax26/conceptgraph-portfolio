import unittest

from app.core.exceptions import GraphStructureError, LLMProviderRateLimitError
from app.services.provider_metrics import ProviderMetrics


class ProviderMetricsTests(unittest.TestCase):
    def test_success_records_sanitized_usage_and_latency(self):
        metrics = ProviderMetrics()
        secret_prompt = "private syllabus text API_KEY=do-not-store"

        result = metrics.observe(
            "groq",
            "answer_synthesis",
            lambda: "grounded answer",
            input_characters=len(secret_prompt),
        )

        self.assertEqual(result, "grounded answer")
        snapshot = metrics.snapshot()
        self.assertEqual(snapshot["totals"]["provider_attempts"], 1)
        self.assertEqual(snapshot["totals"]["successful_attempts"], 1)
        self.assertGreater(snapshot["totals"]["estimated_input_tokens"], 0)
        self.assertGreater(snapshot["totals"]["estimated_output_tokens"], 0)
        self.assertNotIn(secret_prompt, str(snapshot))
        self.assertNotIn("grounded answer", str(snapshot))

    def test_rate_limit_and_failover_are_counted(self):
        metrics = ProviderMetrics()

        with self.assertRaises(LLMProviderRateLimitError):
            metrics.observe(
                "groq",
                "graph_extraction",
                lambda: (_ for _ in ()).throw(LLMProviderRateLimitError("quota")),
                input_characters=400,
            )
        metrics.observe(
            "gemini",
            "graph_extraction",
            lambda: "fallback output",
            input_characters=400,
            failover=True,
        )

        totals = metrics.snapshot()["totals"]
        self.assertEqual(totals["provider_attempts"], 2)
        self.assertEqual(totals["successful_attempts"], 1)
        self.assertEqual(totals["failed_attempts"], 1)
        self.assertEqual(totals["failover_attempts"], 1)
        self.assertEqual(totals["rate_limit_errors"], 1)

    def test_invalid_structure_and_evidence_fallback_are_visible(self):
        metrics = ProviderMetrics()

        with self.assertRaises(GraphStructureError):
            metrics.observe(
                "gemini",
                "graph_extraction",
                lambda: (_ for _ in ()).throw(GraphStructureError("invalid graph")),
                input_characters=100,
                failover=True,
            )
        metrics.record_evidence_fallback()

        snapshot = metrics.snapshot()
        self.assertEqual(snapshot["recent_events"][0]["outcome"], "invalid_response")
        self.assertEqual(snapshot["totals"]["evidence_fallbacks"], 1)

    def test_bounded_history_and_clear(self):
        metrics = ProviderMetrics(max_events=2)
        for _ in range(3):
            metrics.observe(
                "groq",
                "answer_synthesis",
                lambda: "ok",
                input_characters=4,
            )
        self.assertEqual(metrics.snapshot()["totals"]["provider_attempts"], 2)

        metrics.clear()
        self.assertEqual(metrics.snapshot()["totals"]["provider_attempts"], 0)


if __name__ == "__main__":
    unittest.main()
