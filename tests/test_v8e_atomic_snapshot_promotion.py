"""RED tests for V8e atomic staged-snapshot promotion."""

import json
import tempfile
import unittest
from pathlib import Path

from src.vulnerability.updater import promote_versioned_snapshot, stage_versioned_snapshot


RECORD = {
    "id": "OSV-2025-V8E",
    "aliases": ["CVE-2025-9999"],
    "affected": [{"package": {"ecosystem": "PyPI", "name": "v8e-demo"}}],
}


class TestV8eAtomicSnapshotPromotion(unittest.TestCase):
    def test_promotes_verified_snapshot_by_atomic_active_pointer_switch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            staged = root / "snapshot-v8e"
            active = root / "active"
            stage_versioned_snapshot(json.dumps([RECORD]).encode(), staged, "osv", "snapshot-v8e", "sha256:" + "e" * 64)
            result = promote_versioned_snapshot(staged, active)
            self.assertEqual(result["state"], "active")
            self.assertEqual(result["snapshot_id"], "snapshot-v8e")
            self.assertTrue(active.is_symlink())
            self.assertEqual(active.resolve(), staged.resolve())
            self.assertIsNone(result["previous_snapshot_id"])

    def test_replaces_previous_pointer_only_after_new_snapshot_verifies(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "snapshot-first"
            second = root / "snapshot-second"
            active = root / "active"
            stage_versioned_snapshot(json.dumps([RECORD]).encode(), first, "osv", "snapshot-first", "sha256:" + "1" * 64)
            stage_versioned_snapshot(json.dumps([dict(RECORD, id="OSV-2025-V8E-SECOND")]).encode(), second, "osv", "snapshot-second", "sha256:" + "2" * 64)
            promote_versioned_snapshot(first, active)
            result = promote_versioned_snapshot(second, active)
            self.assertEqual(result["previous_snapshot_id"], "snapshot-first")
            self.assertEqual(active.resolve(), second.resolve())

    def test_tampered_snapshot_does_not_change_existing_active_pointer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "snapshot-first"
            bad = root / "snapshot-bad"
            active = root / "active"
            stage_versioned_snapshot(json.dumps([RECORD]).encode(), first, "osv", "snapshot-first", "sha256:" + "3" * 64)
            stage_versioned_snapshot(json.dumps([dict(RECORD, id="OSV-2025-V8E-BAD")]).encode(), bad, "osv", "snapshot-bad", "sha256:" + "4" * 64)
            promote_versioned_snapshot(first, active)
            manifest = bad / "manifest.json"
            data = json.loads(manifest.read_text(encoding="utf-8"))
            data["advisory_count"] = 999
            manifest.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(ValueError):
                promote_versioned_snapshot(bad, active)
            self.assertEqual(active.resolve(), first.resolve())

    def test_regular_active_path_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            staged = root / "snapshot-v8e"
            active = root / "active"
            active.write_text("keep", encoding="utf-8")
            stage_versioned_snapshot(json.dumps([RECORD]).encode(), staged, "osv", "snapshot-v8e", "sha256:" + "5" * 64)
            with self.assertRaises(FileExistsError):
                promote_versioned_snapshot(staged, active)
            self.assertEqual(active.read_text(encoding="utf-8"), "keep")


if __name__ == "__main__":
    unittest.main()
