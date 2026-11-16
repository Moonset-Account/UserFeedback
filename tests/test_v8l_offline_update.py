"""RED tests for V8l offline update orchestration."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from src.vulnerability.updater import stage_versioned_snapshot


class TestV8lOfflineUpdate(unittest.TestCase):
    def _feed(self, directory: Path) -> Path:
        path = directory / "osv.json"
        path.write_text(json.dumps([{"id": "OSV-V8L-1", "affected": []}]) + "\n", encoding="utf-8")
        return path

    def test_update_stages_and_verifies_local_input_without_activation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "snapshots"
            root.mkdir()
            active_snapshot = root / "snapshot-active"
            active = Path(directory) / "active"
            stage_versioned_snapshot(json.dumps([{"id": "OSV-V8L-A", "affected": []}]).encode(), active_snapshot, "osv", "snapshot-active", "sha256:" + "a" * 64)
            active.symlink_to(active_snapshot, target_is_directory=True)
            feed = self._feed(Path(directory))
            result = subprocess.run(
                [sys.executable, "-m", "src", "vuln-db", "update", "--input", str(feed), "--root", str(root), "--source-id", "osv", "--snapshot-id", "snapshot-staged", "--active", str(active)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["state"], "staged")
            self.assertFalse(report["activated"])
            self.assertEqual(active.resolve(), active_snapshot.resolve())
            self.assertTrue((root / "snapshot-staged" / "manifest.json").is_file())

    def test_update_requires_apply_for_activation_and_switches_only_with_apply(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "snapshots"
            root.mkdir()
            active_snapshot = root / "snapshot-active"
            active = Path(directory) / "active"
            stage_versioned_snapshot(json.dumps([{"id": "OSV-V8L-A", "affected": []}]).encode(), active_snapshot, "osv", "snapshot-active", "sha256:" + "a" * 64)
            active.symlink_to(active_snapshot, target_is_directory=True)
            feed = self._feed(Path(directory))
            result = subprocess.run(
                [sys.executable, "-m", "src", "vuln-db", "update", "--input", str(feed), "--root", str(root), "--source-id", "osv", "--snapshot-id", "snapshot-applied", "--active", str(active), "--apply"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["state"], "active")
            self.assertTrue(report["activated"])
            self.assertEqual(report["previous_snapshot_id"], "snapshot-active")
            self.assertEqual(active.resolve(), (root / "snapshot-applied").resolve())

    def test_update_rejects_symlink_input_and_urls(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "snapshots"
            root.mkdir()
            real = self._feed(Path(directory))
            link = Path(directory) / "feed-link.json"
            link.symlink_to(real)
            for input_value in (str(link), "https://example.test/osv.json"):
                result = subprocess.run(
                    [sys.executable, "-m", "src", "vuln-db", "update", "--input", input_value, "--root", str(root), "--source-id", "osv", "--snapshot-id", "snapshot-rejected"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 3)
                self.assertIn("rejected", result.stderr)


if __name__ == "__main__":
    unittest.main()
