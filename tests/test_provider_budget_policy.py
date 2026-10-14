import unittest

from pydantic import ValidationError

from app.core.config import Settings
from app.core.exceptions import LLMProviderRateLimitError, SharedProviderBudgetExceeded
from app.services.provider_budget_policy import BudgetUsage, admission_denial, estimated_reservation
from app.services.provider_failover import ProviderCircuitBreaker


def config(**values):
    return Settings(_env_file=None, RERANK_PROVIDER="local", **values)


class ProviderBudgetPolicyTests(unittest.TestCase):
    def test_defaults_keep_old_request_limits_and_disable_token_limits(self):
        cfg = config()
        self.assertIsNone(admission_denial(cfg, BudgetUsage(9, 299, 10**12, 10**12),
                                         operation="graph_extraction", estimated_tokens=10**12, epoch=1))
        self.assertIsNotNone(admission_denial(cfg, BudgetUsage(10, 299),
                                            operation="answer_synthesis", estimated_tokens=0, epoch=1))

    def test_background_cannot_spend_interactive_request_holdback(self):
        cfg = config(PROVIDER_INTERACTIVE_REQUEST_RESERVE_PER_MINUTE=2)
        usage = BudgetUsage(8, 8)
        self.assertEqual(admission_denial(cfg, usage, operation="graph_extraction", estimated_tokens=0, epoch=61),
                         ("minute request", 59))
        self.assertIsNone(admission_denial(cfg, usage, operation="answer_synthesis", estimated_tokens=0, epoch=61))

    def test_interactive_cannot_exceed_total_request_limit(self):
        cfg = config(PROVIDER_INTERACTIVE_REQUEST_RESERVE_PER_DAY=20)
        self.assertIsNotNone(admission_denial(cfg, BudgetUsage(0, 300),
                                            operation="answer_synthesis", estimated_tokens=0, epoch=61))

    def test_token_holdback_and_exact_boundary(self):
        cfg = config(PROVIDER_ESTIMATED_TOKENS_PER_MINUTE=5000, PROVIDER_INTERACTIVE_TOKEN_RESERVE_PER_MINUTE=1000)
        self.assertIsNone(admission_denial(cfg, BudgetUsage(0, 0, 3000, 3000),
                                         operation="graph_extraction", estimated_tokens=1000, epoch=61))
        usage = BudgetUsage(0, 0, 4000, 4000)
        self.assertIsNotNone(admission_denial(cfg, usage, operation="graph_extraction", estimated_tokens=1, epoch=61))
        self.assertIsNone(admission_denial(cfg, usage, operation="answer_synthesis", estimated_tokens=1000, epoch=61))
        self.assertIsNotNone(admission_denial(cfg, usage, operation="answer_synthesis", estimated_tokens=1001, epoch=61))

    def test_retry_waits_for_every_exceeded_window(self):
        cfg = config(PROVIDER_ESTIMATED_TOKENS_PER_DAY=100)
        reason, retry = admission_denial(cfg, BudgetUsage(10, 0, 0, 100),
                                        operation="answer_synthesis", estimated_tokens=1, epoch=61)
        self.assertEqual(reason, "day estimated-token")
        self.assertEqual(retry, 86400 - 61)

    def test_utc_boundary_never_returns_zero_retry(self):
        self.assertEqual(admission_denial(config(), BudgetUsage(10, 0),
                                         operation="answer_synthesis", estimated_tokens=0, epoch=119.9)[1], 1)

    def test_operation_allowances_are_explicit_estimates(self):
        self.assertEqual(estimated_reservation(5, "answer_synthesis"), 1202)
        self.assertEqual(estimated_reservation(5, "graph_extraction"), 2002)
        self.assertEqual(estimated_reservation(0, "other"), 0)

    def test_invalid_input_counts_are_rejected(self):
        for value in (-1, True, 1.5, "10"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                estimated_reservation(value, "answer_synthesis")

    def test_invalid_reserve_configuration_is_rejected(self):
        cases = [
            {"PROVIDER_INTERACTIVE_REQUEST_RESERVE_PER_MINUTE": 10},
            {"PROVIDER_INTERACTIVE_REQUEST_RESERVE_PER_DAY": 300},
            {"PROVIDER_INTERACTIVE_TOKEN_RESERVE_PER_MINUTE": 1},
            {"PROVIDER_ESTIMATED_TOKENS_PER_DAY": 100, "PROVIDER_INTERACTIVE_TOKEN_RESERVE_PER_DAY": 100},
        ]
        for values in cases:
            with self.subTest(values=values), self.assertRaises(ValidationError):
                config(**values)

    def test_background_embedding_classification_does_not_claim_integration(self):
        cfg = config(PROVIDER_INTERACTIVE_REQUEST_RESERVE_PER_MINUTE=2)
        self.assertIsNotNone(admission_denial(cfg, BudgetUsage(8, 8),
                                            operation="document_embedding", estimated_tokens=0, epoch=61))

    def test_local_budget_denial_does_not_open_vendor_circuit(self):
        breaker = ProviderCircuitBreaker()
        breaker.block("groq", 300, SharedProviderBudgetExceeded("background holdback", retry_after_seconds=3600))
        self.assertTrue(breaker.is_available("groq"))
        self.assertIsNone(breaker.status("groq")["last_outcome"])

    def test_local_budget_denial_does_not_clear_existing_vendor_cooldown(self):
        breaker = ProviderCircuitBreaker()
        breaker.block("groq", 300, LLMProviderRateLimitError("actual vendor quota", retry_after_seconds=3600))
        previous = breaker.status("groq")
        breaker.block("groq", 300, SharedProviderBudgetExceeded("local limit", retry_after_seconds=7200))
        self.assertFalse(breaker.is_available("groq"))
        self.assertEqual(breaker.status("groq")["last_failure_category"], previous["last_failure_category"])
