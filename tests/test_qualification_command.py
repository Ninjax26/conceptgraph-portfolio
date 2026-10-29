"""Published qualification commands must include their selected prerequisites."""
from types import SimpleNamespace
import unittest
from scripts.evaluate_upload_deletion import qualification_command


class QualificationCommandTests(unittest.TestCase):
    def test_transition_command_includes_every_selected_prerequisite(self):
        flags = dict(projection_fencing=True, worker_crashes=True, full_lifecycle=True,
            vector_rollout_audit=True, fleet_inventory=True, drain_rollback=True)
        command = qualification_command(SimpleNamespace(**flags))
        self.assertTrue(command.startswith(".venv/bin/python scripts/evaluate_upload_deletion.py --socket-dir "))
        self.assertEqual(command.split()[4:], ["--projection-fencing", "--worker-crashes",
            "--full-lifecycle", "--vector-rollout-audit", "--fleet-inventory", "--drain-rollback"])

    def test_unselected_modes_are_not_reported_as_executed(self):
        flags = dict(projection_fencing=False, worker_crashes=False, full_lifecycle=False,
            vector_rollout_audit=False, fleet_inventory=False, drain_rollback=False)
        command = qualification_command(SimpleNamespace(**flags))
        self.assertEqual(command, ".venv/bin/python scripts/evaluate_upload_deletion.py --socket-dir <private-test-socket>")
