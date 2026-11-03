import unittest
from app.core.config import Settings
from scripts.graph_runtime_checks import runtime_config

class GraphRuntimeFixtureTests(unittest.TestCase):
    def test_requires_explicit_runtime_role_and_preserves_fencing_and_priority(self):
        base=Settings(_env_file=None)
        for role in ('worker','combined'):
            cfg=runtime_config(base,role)
            self.assertEqual(cfg.processing_role,role)
            self.assertTrue(cfg.graph_continuation_dispatch_enabled)
            self.assertTrue(cfg.graph_continuation_maintenance_enabled)
            self.assertTrue(cfg.graph_projection_fencing_enabled)
            self.assertTrue(cfg.shared_provider_limits_enabled)
            self.assertGreater(cfg.provider_interactive_request_reserve_per_minute,0)
            self.assertEqual(cfg.processing_lease_seconds,60)
        self.assertFalse(base.graph_projection_fencing_enabled)
        for role in ('api','bad',None):
            with self.assertRaises(ValueError): runtime_config(base,role)

    def test_report_distinguishes_direct_publisher_and_runtime_lease_coverage(self):
        from scripts.evaluate_graph_publication_recovery import recovery_limits
        regular=' '.join(recovery_limits(False))
        runtime=' '.join(recovery_limits(True))
        self.assertIn('fixture lease',regular)
        self.assertIn('natural 60-second',runtime)
        self.assertIn('startup validation',runtime)
        self.assertIn('no HTTP',runtime)
        self.assertNotIn('no natural lease-duration',runtime)
