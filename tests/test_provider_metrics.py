import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from app.core.exceptions import GraphStructureError, LLMProviderRateLimitError
from app.services.provider_metrics import ProviderMetrics, record_provider_usage


class ProviderMetricsTests(unittest.TestCase):
    def test_concurrent_threads_do_not_mix_reported_usage(self):
        metrics = ProviderMetrics()
        barrier = Barrier(2)
        def measured(provider, count):
            def generate():
                record_provider_usage(count, count, count*2)
                barrier.wait(timeout=5)
                return "ok"
            return metrics.observe(provider, "fixture", generate, input_characters=4)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(measured, "groq", 3), pool.submit(measured, "gemini", 7)]
            self.assertEqual([future.result(timeout=10) for future in futures], ["ok", "ok"])
        totals = {row["name"]: row["reported_total_tokens"] for row in metrics.snapshot()["by_provider"]}
        self.assertEqual(totals, {"groq":6, "gemini":14})

    def test_groq_synthesis_adapter_captures_response_usage(self):
        from app.services.synthesis_service import SynthesisService
        client = Mock()
        client.chat.completions.create.return_value = SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=12, completion_tokens=7, total_tokens=19),
            choices=[SimpleNamespace(message=SimpleNamespace(content="Supported answer"))])
        metrics = ProviderMetrics()
        with patch("app.services.synthesis_service.Groq", return_value=client):
            answer = metrics.observe("groq", "answer_synthesis",
                lambda: SynthesisService()._synthesize_with_groq("question", [], []), input_characters=4)
        self.assertEqual(answer, "Supported answer")
        self.assertEqual(metrics.snapshot()["totals"]["reported_total_tokens"], 19)

    def test_groq_truncated_graph_still_captures_consumed_usage(self):
        from app.services.ingestion_service import IngestionService
        client = Mock()
        client.chat.completions.create.return_value = SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=12, completion_tokens=7, total_tokens=19),
            choices=[SimpleNamespace(finish_reason="length", message=SimpleNamespace(content="{}"))])
        metrics = ProviderMetrics()
        with patch("app.services.ingestion_service.Groq", return_value=client), self.assertRaises(GraphStructureError):
            metrics.observe("groq", "graph_extraction", lambda: IngestionService(
                graph_driver=SimpleNamespace(), vector_client=SimpleNamespace())._extract_with_groq(
                    "evidence", allow_repair=False), input_characters=4)
        self.assertEqual(metrics.snapshot()["totals"]["reported_total_tokens"], 19)
        client.close.assert_called_once()

    def test_provider_reported_counts_are_separate_from_estimates(self):
        metrics = ProviderMetrics()
        def generate():
            record_provider_usage(12, 7, 19)
            return "ok"
        metrics.observe("groq", "fixture", generate, input_characters=400)
        totals = metrics.snapshot()["totals"]
        self.assertEqual(totals["reported_total_tokens"], 19)
        self.assertEqual(totals["reported_usage_attempts"], 1)
        self.assertEqual(totals["estimated_input_tokens"], 100)
        self.assertEqual(metrics.snapshot()["by_provider"][0]["reported_total_tokens"], 19)

    def test_failed_validation_keeps_usage_without_leaking_into_next_call(self):
        metrics = ProviderMetrics()
        def generate():
            record_provider_usage(12, 7, 19)
            raise GraphStructureError("bad output")
        with self.assertRaises(GraphStructureError):
            metrics.observe("groq", "fixture", generate, input_characters=4)
        metrics.observe("groq", "fixture", lambda: "ok", input_characters=4)
        events = metrics.snapshot()["recent_events"]
        self.assertEqual(events[0]["reported_total_tokens"], 19)
        self.assertIsNone(events[1]["reported_total_tokens"])
        self.assertEqual(metrics.snapshot()["totals"]["unreported_usage_attempts"], 1)

    def test_invalid_or_outside_call_usage_is_not_recorded(self):
        record_provider_usage(99, 99, 198)
        metrics = ProviderMetrics()
        def generate():
            record_provider_usage(True, 1, 2)
            record_provider_usage(1, -1, 0)
            record_provider_usage(None, 1, 2)
            return "ok"
        metrics.observe("groq", "fixture", generate, input_characters=4)
        self.assertEqual(metrics.snapshot()["totals"]["reported_usage_attempts"], 0)

    def test_nested_measurements_keep_usage_isolated(self):
        outer, inner = ProviderMetrics(), ProviderMetrics()
        def generate():
            record_provider_usage(2, 3, 5)
            inner.observe("groq", "inner", lambda: record_provider_usage(7, 8, 15), input_characters=4)
            record_provider_usage(1, 1, 2)
            return "ok"
        outer.observe("groq", "outer", generate, input_characters=4)
        self.assertEqual(outer.snapshot()["totals"]["reported_total_tokens"], 7)
        self.assertEqual(inner.snapshot()["totals"]["reported_total_tokens"], 15)

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
