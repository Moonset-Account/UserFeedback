"""RED tests for V8k explicit rollback CLI."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from src.vulnerability.updater import promote_versioned_snapshot, stage_versioned_snapshot


class TestV8kRollbackCLI(unittest.TestCase):
    def _make(self, root: Path, name: str, marker: str) -> Path:
        path = root / name
        stage_versioned_snapshot(
            json.dumps([{"id": f"OSV-V8K-{marker}", "affected": []}]).encode(),
            path,
            "osv",
            name,
            "sha256:" + marker * 64,
        )
        return path

    def test_rollback_requires_apply_and_does_not_mutate_without_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            active_snapshot = self._make(root, "snapshot-active", "a")
            target = self._make(root, "snapshot-target", "b")
            active = root / "active"
            promote_versioned_snapshot(active_snapshot, active)
            result = subprocess.run(
                [sys.executable, "-m", "src", "vuln-db", "rollback", "--active", str(active), "--target", str(target)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 3)
            self.assertIn("--apply", result.stderr)
            self.assertEqual(active.resolve(), active_snapshot.resolve())

    def test_rollback_apply_switches_pointer_and_reports_previous_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            active_snapshot = self._make(root, "snapshot-active", "c")
            target = self._make(root, "snapshot-target", "d")
            active = root / "active"
            promote_versioned_snapshot(active_snapshot, active)
            result = subprocess.run(
                [sys.executable, "-m", "src", "vuln-db", "rollback", "--active", str(active), "--target", str(target), "--apply"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["state"], "rolled_back")
            self.assertEqual(report["snapshot_id"], "snapshot-target")
            self.assertEqual(report["previous_snapshot_id"], "snapshot-active")
            self.assertEqual(active.resolve(), target.resolve())
            self.assertTrue(active_snapshot.exists())

    def test_rollback_rejects_same_target_without_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            active_snapshot = self._make(root, "snapshot-active", "e")
            active = root / "active"
            promote_versioned_snapshot(active_snapshot, active)
            result = subprocess.run(
                [sys.executable, "-m", "src", "vuln-db", "rollback", "--active", str(active), "--target", str(active_snapshot), "--apply"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 3)
            self.assertEqual(active.resolve(), active_snapshot.resolve())


if __name__ == "__main__":
    unittest.main()
