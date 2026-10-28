"""Offline contracts for the full-entry-point qualification adapters."""
from types import SimpleNamespace
from pathlib import Path
import tempfile
import unittest

from app.core.config import Settings
from scripts.worker_lifecycle_checks import lifecycle_config, OfflineReranker, OfflineSynthesis, ShutdownReceipt


class WorkerLifecycleFixtureTests(unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_receipt_requires_actual_asgi_shutdown_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            receipt = Path(directory)/"closed"
            sent = []
            async def send(message):
                sent.append(message)
            async def app(scope, receive, observed_send):
                await observed_send({"type": "lifespan.startup.complete"})
                self.assertFalse(receipt.exists())
                await observed_send({"type": "lifespan.shutdown.complete"})
            await ShutdownReceipt(app, receipt)({"type": "lifespan"}, None, send)
            self.assertEqual(receipt.read_text(), "lifespan.shutdown.complete")
            self.assertEqual(len(sent), 2)

    def test_fixture_roles_use_shared_budgets_and_valid_minimum_timer(self):
        base = Settings(_env_file=None, GRAPH_PROJECTION_FENCING_ENABLED=True)
        for role in ("api", "worker"):
            cfg = lifecycle_config(base, role)
            self.assertEqual(cfg.processing_role, role)
            self.assertTrue(cfg.shared_provider_limits_enabled)
            self.assertEqual(cfg.demo_cleanup_interval_seconds, 60)
            self.assertGreater(cfg.processing_lease_seconds, cfg.processing_heartbeat_seconds*2)
            self.assertEqual(cfg.embedding_provider, "local")
            self.assertFalse(cfg.graph_continuation_snapshots_enabled)
        self.assertEqual(base.processing_role, "combined")

    def test_fixture_rejects_combined_role_and_does_not_enable_production_defaults(self):
        base = Settings(_env_file=None)
        self.assertFalse(base.graph_projection_fencing_enabled)
        self.assertFalse(base.graph_continuation_snapshots_enabled)
        with self.assertRaises(ValueError):
            lifecycle_config(base, "combined")
        with self.assertRaises(ValueError):
            lifecycle_config(base, "worker")

    async def test_offline_reranker_labels_fixed_scores_without_mutating_candidates(self):
        chunks = [{"text": "Synthetic source.", "metadata": {"upload_id": "fixture"}}]
        ranked = await OfflineReranker().rerank("Question", chunks)
        self.assertEqual(ranked[0]["rerank_score"], 10.)
        self.assertNotIn("rerank_score", chunks[0])

    async def test_offline_synthesis_is_an_explicit_cited_excerpt_not_a_provider_result(self):
        generation = await OfflineSynthesis().synthesize_with_metadata(question="Question", graph_context=[],
            sources=[{"supporting_passage": "Synthetic source."}])
        self.assertEqual(generation.provider_used, "evidence_fallback")
        self.assertIn("Synthetic source.", generation.answer)
        self.assertIn("[Source 1]", generation.answer)
        with self.assertRaises(ValueError):
            await OfflineSynthesis().synthesize_with_metadata(question="Question", graph_context=[], sources=[])
