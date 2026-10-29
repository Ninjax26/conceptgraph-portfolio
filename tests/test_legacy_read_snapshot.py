"""Broad reads need a clean snapshot as well as separately proved writer drain."""
import unittest
from copy import deepcopy
from app.services.vector_rollout_audit import legacy_read_snapshot_compatible


class LegacyReadSnapshotTests(unittest.TestCase):
    def snapshot(self):
        return {"scan_completed": True, "read_coverage_ok": True,
            "cleanup_payloads_trackable": True, "error_type": None,
            "counts": dict(scanned=2, current_points=2, current_unique_chunks=2,
                known_stale_points=0, unknown_task_points=0, untagged_points=0,
                foreign_points=0, malformed_current_points=0, duplicate_current_chunks=0)}

    def test_clean_current_snapshot_is_compatible_without_certifying_drain(self):
        snapshot = self.snapshot()
        before = deepcopy(snapshot)
        self.assertTrue(legacy_read_snapshot_compatible(snapshot))
        self.assertEqual(snapshot, before)
        self.assertNotIn("writer_drain_verified", snapshot)

    def test_stale_unknown_untagged_malformed_duplicates_and_foreign_points_block(self):
        for name in self.snapshot()["counts"]:
            if name in {"scanned", "current_points", "current_unique_chunks"}:
                continue
            with self.subTest(name=name):
                snapshot = self.snapshot()
                snapshot["counts"][name] = 1
                self.assertFalse(legacy_read_snapshot_compatible(snapshot))

    def test_incomplete_erroneous_or_malformed_snapshots_fail_closed(self):
        for field, value in (("scan_completed", False), ("read_coverage_ok", 1),
                ("cleanup_payloads_trackable", False), ("error_type", "ReadError"),
                ("counts", None)):
            snapshot = self.snapshot()
            snapshot[field] = value
            self.assertFalse(legacy_read_snapshot_compatible(snapshot))
        for value in (None, [], {}, {"counts": {}}):
            self.assertFalse(legacy_read_snapshot_compatible(value))
        for value in (True, -1, 0, "2", 3):
            snapshot = self.snapshot()
            snapshot["counts"]["scanned"] = value
            self.assertFalse(legacy_read_snapshot_compatible(snapshot))
