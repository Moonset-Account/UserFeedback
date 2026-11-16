"""RED tests for V8m read-only source-status CLI."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from src.vulnerability.updater import stage_versioned_snapshot


class TestV8mSourceStatus(unittest.TestCase):
    def _stage(self, root: Path, snapshot_id: str, source_id: str, marker: str) -> Path:
        path = root / snapshot_id
        stage_versioned_snapshot(
            json.dumps([{"id": f"OSV-V8M-{marker}", "affected": []}]).encode(),
            path,
            source_id,
            snapshot_id,
            "sha256:" + marker * 64,
        )
        return path

    def test_reports_deterministic_verified_source_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "snapshots"
            root.mkdir()
            active_snapshot = self._stage(root, "snapshot-osv", "osv", "a")
            self._stage(root, "snapshot-ghsa", "ghsa", "b")
            active = Path(directory) / "active"
            active.symlink_to(active_snapshot, target_is_directory=True)
            command = [sys.executable, "-m", "src", "vuln-db", "source-status", "--root", str(root), "--active", str(active)]
            first = subprocess.run(command, capture_output=True, text=True, check=False)
            second = subprocess.run(command, capture_output=True, text=True, check=False)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)
            report = json.loads(first.stdout)
            self.assertEqual(report, json.loads(second.stdout))
            self.assertEqual(report["state"], "ok")
            self.assertEqual(report["active_snapshot_id"], "snapshot-osv")
            self.assertEqual(report["sources"][0]["source_id"], "ghsa")
            self.assertEqual(report["sources"][1]["source_id"], "osv")
            self.assertTrue(report["sources"][1]["active"])
            self.assertEqual(report["sources"][0]["snapshot_count"], 1)
            self.assertNotIn("record_json", first.stdout)

    def test_reconciliation_issue_returns_exit_three(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "snapshots"
            root.mkdir()
            active_snapshot = self._stage(root, "snapshot-osv", "osv", "c")
            active = Path(directory) / "active"
            active.symlink_to(active_snapshot, target_is_directory=True)
            (root / "unexpected").write_text("not a snapshot", encoding="utf-8")
            result = subprocess.run(
                [sys.executable, "-m", "src", "vuln-db", "source-status", "--root", str(root), "--active", str(active)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 3)
            report = json.loads(result.stdout)
            self.assertEqual(report["state"], "reconciliation_failed")
            self.assertEqual(report["issue_count"], 1)


if __name__ == "__main__":
    unittest.main()
