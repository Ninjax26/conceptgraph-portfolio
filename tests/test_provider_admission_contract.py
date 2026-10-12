"""Offline characterization of admission boundaries before Phase 3 changes."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from app.core.exceptions import LLMProviderUnavailableError, SharedProviderBudgetExceeded
from app.services import shared_provider_service as shared
from app.services.provider_failover import provider_circuit_breaker


class ProviderAdmissionContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_reservation_precedes_call_and_keeps_failover_repair_metadata(self):
        events = []

        async def reserve(provider, **metadata):
            self.assertEqual(metadata, {"operation": "graph_extraction", "estimated_tokens": 2011})
            events.append(("reserved", provider))

        def observe(provider, operation, generate, **metadata):
            events.append(("called", provider))
            self.assertEqual(operation, "graph_extraction")
            self.assertEqual(metadata, {"input_characters": 42, "failover": True, "repair": True})
            return generate()

        counters = {}
        token = shared.provider_call_counts.set(counters)
        generate = Mock(return_value="validated result")
        try:
            with patch.object(shared, "reserve_provider_request", AsyncMock(side_effect=reserve)), \
                 patch.object(shared.provider_metrics, "observe", side_effect=observe):
                result = await shared.observe_provider_call(
                    "gemini", "graph_extraction", generate,
                    input_characters=42, failover=True, repair=True,
                )
            self.assertEqual(result, "validated result")
            self.assertEqual(events, [("reserved", "gemini"), ("called", "gemini")])
            self.assertEqual(counters, {"graph_extraction": 1})
            generate.assert_called_once_with()
        finally:
            shared.provider_call_counts.reset(token)

    async def test_cancelled_admission_does_not_send_or_count_a_call(self):
        counters = {}
        token = shared.provider_call_counts.set(counters)
        generate = Mock()
        try:
            with patch.object(shared, "reserve_provider_request", AsyncMock(side_effect=asyncio.CancelledError)), \
                 patch.object(shared.provider_metrics, "observe") as observe:
                with self.assertRaises(asyncio.CancelledError):
                    await shared.observe_provider_call("groq", "answer", generate, input_characters=42)
            generate.assert_not_called()
            observe.assert_not_called()
            self.assertEqual(counters, {})
        finally:
            shared.provider_call_counts.reset(token)

    async def test_budget_storage_failure_prevents_a_vendor_request(self):
        generate = Mock()
        with patch.object(shared.settings, "shared_provider_limits_enabled", True), \
             patch.object(shared, "AsyncSessionLocal", side_effect=RuntimeError("database unavailable")), \
             patch.object(shared.provider_metrics, "observe") as observe:
            with self.assertRaises(LLMProviderUnavailableError) as raised:
                await shared.observe_provider_call("groq", "answer", generate, input_characters=42)
        self.assertIsInstance(raised.exception.__cause__, RuntimeError)
        generate.assert_not_called()
        observe.assert_not_called()

    async def test_graph_holdback_does_not_block_next_interactive_answer(self):
        from app.services.ingestion_service import IngestionService
        from app.services.synthesis_service import SynthesisService

        provider_circuit_breaker.clear()
        self.addCleanup(provider_circuit_breaker.clear)
        graph = IngestionService(SimpleNamespace(), SimpleNamespace())
        answer = SynthesisService()

        async def admission(provider, *, operation, estimated_tokens):
            if operation == "graph_extraction":
                raise SharedProviderBudgetExceeded("background holdback", retry_after_seconds=3600)

        with patch.object(shared, "reserve_provider_request", AsyncMock(side_effect=admission)), \
             patch.object(shared.settings, "groq_api_key", "fixture-only"), \
             patch.object(shared.settings, "gemini_api_key", None), \
             patch.object(shared.settings, "cerebras_api_key", None), \
             patch.object(graph, "_extract_with_groq") as extract, \
             patch.object(answer, "_synthesize_with_groq", return_value="Supported fixture answer. [Source 1]") as generate:
            with self.assertRaises(SharedProviderBudgetExceeded):
                await graph._extract_with_failover("fixture text")
            extract.assert_not_called()
            self.assertTrue(provider_circuit_breaker.is_available("groq"))
            result = await answer._synthesize_with_failover_metadata("Fixture question?", [], [])
        self.assertEqual(result.provider_used, "groq")
        self.assertFalse(result.failover_used)
        generate.assert_called_once()
