"""RED tests for V8g safe versioned-snapshot retention pruning."""

import json
import tempfile
import unittest
from pathlib import Path

from src.vulnerability.updater import (
    promote_versioned_snapshot,
    prune_versioned_snapshots,
    stage_versioned_snapshot,
)


RECORD = {"id": "OSV-2025-V8G", "affected": [{"package": {"ecosystem": "PyPI", "name": "v8g-demo"}}]}


class TestV8gRetentionPruning(unittest.TestCase):
    def _make(self, root, name, digest):
        path = root / name
        stage_versioned_snapshot(json.dumps([dict(RECORD, id=f"{RECORD['id']}-{name}")]).encode(), path, "osv", name, "sha256:" + digest * 64)
        return path

    def test_dry_run_reports_only_unprotected_snapshots(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            active_snapshot = self._make(root, "snapshot-active", "1")
            retained = self._make(root, "snapshot-retained", "2")
            removable = self._make(root, "snapshot-removable", "3")
            active = root / "active"
            promote_versioned_snapshot(active_snapshot, active)
            report = prune_versioned_snapshots(root, active, keep_snapshot_ids={retained.name})
            self.assertFalse(report["applied"])
            self.assertEqual(report["deletable_snapshot_ids"], [removable.name])
            self.assertTrue(removable.exists())
            self.assertEqual(active.resolve(), active_snapshot.resolve())

    def test_apply_deletes_only_unprotected_verified_snapshots(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            active_snapshot = self._make(root, "snapshot-active", "4")
            retained = self._make(root, "snapshot-retained", "5")
            removable = self._make(root, "snapshot-removable", "6")
            active = root / "active"
            promote_versioned_snapshot(active_snapshot, active)
            report = prune_versioned_snapshots(root, active, keep_snapshot_ids={retained.name}, apply=True)
            self.assertTrue(report["applied"])
            self.assertEqual(report["deleted_snapshot_ids"], [removable.name])
            self.assertFalse(removable.exists())
            self.assertTrue(retained.exists())
            self.assertTrue(active_snapshot.exists())
            self.assertEqual(active.resolve(), active_snapshot.resolve())

    def test_rollback_targets_are_protected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            active_snapshot = self._make(root, "snapshot-active", "7")
            rollback_target = self._make(root, "snapshot-rollback", "8")
            removable = self._make(root, "snapshot-removable", "9")
            active = root / "active"
            promote_versioned_snapshot(active_snapshot, active)
            report = prune_versioned_snapshots(root, active, keep_snapshot_ids={rollback_target.name})
            self.assertEqual(report["protected_snapshot_ids"], [active_snapshot.name, rollback_target.name])
            self.assertEqual(report["deletable_snapshot_ids"], [removable.name])

    def test_malformed_snapshot_blocks_pruning_and_preserves_all(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            active_snapshot = self._make(root, "snapshot-active", "a")
            malformed = root / "snapshot-malformed"
            malformed.mkdir()
            (malformed / "manifest.json").write_text("{}", encoding="utf-8")
            active = root / "active"
            promote_versioned_snapshot(active_snapshot, active)
            with self.assertRaises(ValueError):
                prune_versioned_snapshots(root, active, apply=True)
            self.assertTrue(malformed.exists())
            self.assertTrue(active_snapshot.exists())

    def test_unknown_keep_id_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            active_snapshot = self._make(root, "snapshot-active", "b")
            active = root / "active"
            promote_versioned_snapshot(active_snapshot, active)
            with self.assertRaises(ValueError):
                prune_versioned_snapshots(root, active, keep_snapshot_ids={"snapshot-missing"})


if __name__ == "__main__":
    unittest.main()
