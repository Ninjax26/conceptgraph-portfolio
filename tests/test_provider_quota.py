import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
from groq import AuthenticationError
from pydantic import SecretStr

from app.core.exceptions import GraphStructureError, LLMProviderRateLimitError, LLMProviderRequestError
from app.schemas.extraction import GraphExtractionResponse
from app.services.cerebras_service import CerebrasService
from app.services.ingestion_service import IngestionService
from app.services.provider_failover import ProviderCircuitBreaker, provider_circuit_breaker, retry_after_seconds
from app.services.synthesis_service import SynthesisService


class ProviderQuotaTests(unittest.TestCase):
    def setUp(self):
        provider_circuit_breaker.clear()
        self.addCleanup(provider_circuit_breaker.clear)

    def test_retry_after_numeric_date_and_invalid(self):
        self.assertEqual(retry_after_seconds({"retry-after": "3600"}), 3600)
        with patch("app.services.provider_failover.time.time", return_value=0):
            self.assertEqual(retry_after_seconds({"retry-after": "Thu, 01 Jan 1970 01:00:00 GMT"}), 3600)
        for value in ("bad", "nan", "inf", ""):
            self.assertIsNone(retry_after_seconds({"retry-after": value}))

    def test_cooldown_honors_reset_and_cannot_be_shortened(self):
        breaker = ProviderCircuitBreaker()
        error = LLMProviderRateLimitError("quota", retry_after_seconds=3600)
        with patch("app.services.provider_failover.time.monotonic", return_value=100):
            breaker.block("groq", 300, error)
            breaker.block("groq", 300)
        with patch("app.services.provider_failover.time.monotonic", return_value=401):
            self.assertFalse(breaker.is_available("groq"))
            self.assertTrue(breaker.is_available("cerebras"))
        with patch("app.services.provider_failover.time.monotonic", return_value=3700):
            self.assertTrue(breaker.is_available("groq"))

    def test_sdk_error_response_headers_are_respected(self):
        error = RuntimeError("quota")
        error.response = httpx.Response(429, headers={"Retry-After": "7200"})
        breaker = ProviderCircuitBreaker()
        with patch("app.services.provider_failover.time.monotonic", return_value=0):
            breaker.block("groq", 300, error)
        with patch("app.services.provider_failover.time.monotonic", return_value=3600):
            self.assertFalse(breaker.is_available("groq"))

    def test_cerebras_preserves_reset_without_exposing_body(self):
        with httpx.Client(base_url="https://example.test/v1", transport=httpx.MockTransport(
            lambda request: httpx.Response(429, headers={"Retry-After": "7200"}, text="private")
        )) as client, patch("app.services.cerebras_service.settings.cerebras_api_key", SecretStr("test")):
            with self.assertRaises(LLMProviderRateLimitError) as raised:
                CerebrasService(client).complete([])
        self.assertEqual(raised.exception.retry_after_seconds, 7200)
        self.assertNotIn("private", str(raised.exception))

    def test_secondary_quota_is_not_hidden_by_primary_schema_error(self):
        service = IngestionService(graph_driver=SimpleNamespace(), vector_client=SimpleNamespace())
        with patch("app.services.ingestion_service.settings.groq_api_key", "test"), \
             patch("app.services.cerebras_service.settings.cerebras_api_key", SecretStr("test")), \
             patch.object(service, "_extract_with_groq", side_effect=GraphStructureError("schema")), \
             patch.object(service, "_extract_with_cerebras", side_effect=LLMProviderRateLimitError("quota")):
            with self.assertRaises(LLMProviderRateLimitError):
                asyncio.run(service._extract_with_failover("text"))
        self.assertFalse(provider_circuit_breaker.is_available("cerebras"))

    def test_blocked_groq_is_skipped_and_cerebras_used(self):
        provider_circuit_breaker.block("groq", 3600)
        service = IngestionService(graph_driver=SimpleNamespace(), vector_client=SimpleNamespace())
        with patch("app.services.ingestion_service.settings.groq_api_key", "test"), \
             patch("app.services.cerebras_service.settings.cerebras_api_key", SecretStr("test")), \
             patch.object(service, "_extract_with_groq") as groq, \
             patch.object(service, "_extract_with_cerebras", return_value=GraphExtractionResponse()) as backup:
            asyncio.run(service._extract_with_failover("text"))
        groq.assert_not_called()
        backup.assert_called_once()

    def test_invalid_graph_makes_only_one_groq_call_by_default(self):
        service = IngestionService(graph_driver=SimpleNamespace(), vector_client=SimpleNamespace())
        client = Mock()
        client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="invalid JSON"))])
        with patch("app.services.ingestion_service.settings.groq_api_key", "test"), \
             patch("app.services.ingestion_service.settings.graph_json_repair_enabled", False), \
             patch("app.services.ingestion_service.Groq", return_value=client):
            with self.assertRaises(GraphStructureError):
                service._extract_with_groq("text")
        client.chat.completions.create.assert_called_once()

    def test_invalid_graph_makes_only_one_cerebras_call_by_default(self):
        service = IngestionService(graph_driver=SimpleNamespace(), vector_client=SimpleNamespace())
        with patch("app.services.ingestion_service.settings.graph_json_repair_enabled", False), \
             patch("app.services.ingestion_service.cerebras_service.complete", return_value="invalid JSON") as complete:
            with self.assertRaises(GraphStructureError):
                service._extract_with_cerebras("text")
        complete.assert_called_once()

    def test_rejected_groq_key_still_tries_backup(self):
        error = AuthenticationError("rejected", response=httpx.Response(401,
            request=httpx.Request("POST", "https://example.test")), body=None)
        service = SynthesisService()
        with patch("app.services.synthesis_service.settings.groq_api_key", "test"), \
             patch("app.services.cerebras_service.settings.cerebras_api_key", SecretStr("test")), \
             patch.object(service, "_synthesize_with_groq", side_effect=error), \
             patch.object(service, "_synthesize_with_cerebras", return_value="backup answer") as backup:
            answer = asyncio.run(service._synthesize_with_failover("question", [], []))
        self.assertEqual(answer, "backup answer")
        backup.assert_called_once()
        self.assertFalse(provider_circuit_breaker.is_available("groq"))

    def test_backup_request_error_returns_evidence_instead_of_failing_query(self):
        provider_circuit_breaker.block("groq", 3600)
        service = SynthesisService()
        with patch("app.services.cerebras_service.settings.cerebras_api_key", SecretStr("test")), \
             patch.object(service, "_synthesize_with_cerebras", side_effect=LLMProviderRequestError("HTTP 400")):
            answer = asyncio.run(service._synthesize_with_failover("question", [], [
                {"supporting_passage": "Verified passage", "page_number": 1}]))
        self.assertIn("Verified passage", answer)
