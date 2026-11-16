"""RED tests for V8f explicit snapshot rollback."""

import json
import tempfile
import unittest
from pathlib import Path

from src.vulnerability.updater import (
    promote_versioned_snapshot,
    rollback_versioned_snapshot,
    stage_versioned_snapshot,
    verify_versioned_snapshot,
)


RECORD = {"id": "OSV-2025-V8F", "affected": [{"package": {"ecosystem": "PyPI", "name": "v8f-demo"}}]}


class TestV8fSnapshotRollback(unittest.TestCase):
    def _make(self, root, name, digest, record=RECORD):
        path = root / name
        stage_versioned_snapshot(json.dumps([record]).encode(), path, "osv", name, "sha256:" + digest * 64)
        return path

    def test_rolls_back_to_verified_previous_snapshot_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = self._make(root, "snapshot-first", "1")
            second = self._make(root, "snapshot-second", "2", dict(RECORD, id="OSV-2025-V8F-SECOND"))
            active = root / "active"
            promote_versioned_snapshot(first, active)
            promote_versioned_snapshot(second, active)
            result = rollback_versioned_snapshot(active, first)
            self.assertEqual(result["state"], "rolled_back")
            self.assertEqual(result["snapshot_id"], "snapshot-first")
            self.assertEqual(result["previous_snapshot_id"], "snapshot-second")
            self.assertEqual(active.resolve(), first.resolve())
            self.assertEqual(verify_versioned_snapshot(first)["state"], "staged")

    def test_rejects_same_snapshot_and_keeps_active_pointer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = self._make(root, "snapshot-first", "3")
            active = root / "active"
            promote_versioned_snapshot(first, active)
            with self.assertRaises(ValueError):
                rollback_versioned_snapshot(active, first)
            self.assertEqual(active.resolve(), first.resolve())

    def test_tampered_rollback_target_does_not_change_active_pointer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = self._make(root, "snapshot-first", "4")
            second = self._make(root, "snapshot-second", "5")
            active = root / "active"
            promote_versioned_snapshot(first, active)
            data = json.loads((second / "manifest.json").read_text(encoding="utf-8"))
            data["source_id"] = "tampered"
            (second / "manifest.json").write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(ValueError):
                rollback_versioned_snapshot(active, second)
            self.assertEqual(active.resolve(), first.resolve())

    def test_requires_existing_symlink_active_pointer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = self._make(root, "snapshot-target", "6")
            with self.assertRaises(FileNotFoundError):
                rollback_versioned_snapshot(root / "missing-active", target)


if __name__ == "__main__":
    unittest.main()
