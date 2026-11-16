"""RED tests for V8j list-snapshots and explicit prune CLI."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from src.vulnerability.updater import promote_versioned_snapshot, stage_versioned_snapshot


class TestV8jListPruneCLI(unittest.TestCase):
    def _make(self, root: Path, name: str, marker: str) -> Path:
        path = root / name
        stage_versioned_snapshot(
            json.dumps([{"id": f"OSV-V8J-{marker}", "affected": []}]).encode(),
            path,
            "osv",
            name,
            "sha256:" + marker * 64,
        )
        return path

    def test_list_snapshots_is_deterministic_and_metadata_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            active_snapshot = self._make(root, "snapshot-active", "a")
            self._make(root, "snapshot-older", "b")
            active = root / "active"
            promote_versioned_snapshot(active_snapshot, active)
            result = subprocess.run(
                [sys.executable, "-m", "src", "vuln-db", "list-snapshots", "--root", str(root), "--active", str(active)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["snapshot_ids"], ["snapshot-active", "snapshot-older"])
            self.assertEqual(report["active_snapshot_id"], "snapshot-active")
            self.assertNotIn("record_json", result.stdout)

    def test_prune_defaults_to_dry_run_and_apply_requires_flag(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            active_snapshot = self._make(root, "snapshot-active", "c")
            retained = self._make(root, "snapshot-retained", "d")
            removable = self._make(root, "snapshot-removable", "e")
            active = root / "active"
            promote_versioned_snapshot(active_snapshot, active)
            base = [sys.executable, "-m", "src", "vuln-db", "prune", "--root", str(root), "--active", str(active), "--keep-snapshot-id", retained.name]
            dry_run = subprocess.run(base, capture_output=True, text=True, check=False)
            self.assertEqual(dry_run.returncode, 0, dry_run.stderr)
            dry_report = json.loads(dry_run.stdout)
            self.assertFalse(dry_report["applied"])
            self.assertTrue(removable.exists())
            applied = subprocess.run(base + ["--apply"], capture_output=True, text=True, check=False)
            self.assertEqual(applied.returncode, 0, applied.stderr)
            applied_report = json.loads(applied.stdout)
            self.assertTrue(applied_report["applied"])
            self.assertEqual(applied_report["deleted_snapshot_ids"], [removable.name])
            self.assertFalse(removable.exists())
            self.assertTrue(retained.exists())
            self.assertEqual(active.resolve(), active_snapshot.resolve())

    def test_prune_blocks_malformed_store_without_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            active_snapshot = self._make(root, "snapshot-active", "f")
            malformed = root / "snapshot-malformed"
            malformed.mkdir()
            (malformed / "manifest.json").write_text("{}", encoding="utf-8")
            active = root / "active"
            promote_versioned_snapshot(active_snapshot, active)
            result = subprocess.run(
                [sys.executable, "-m", "src", "vuln-db", "prune", "--root", str(root), "--active", str(active), "--apply"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 3)
            self.assertTrue(malformed.exists())
            self.assertTrue(active_snapshot.exists())
            self.assertEqual(active.resolve(), active_snapshot.resolve())


if __name__ == "__main__":
    unittest.main()
