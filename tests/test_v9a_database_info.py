"""RED tests for V9a read-only database-info."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from src.vulnerability.updater import stage_versioned_snapshot


class TestV9aDatabaseInfo(unittest.TestCase):
    def test_reports_verified_active_manifest_metadata_without_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "snapshots"
            root.mkdir()
            snapshot = root / "snapshot-osv"
            stage_versioned_snapshot(
                json.dumps([{"id": "OSV-V9A-1", "affected": [{"package": {"ecosystem": "PyPI", "name": "demo"}}]}]).encode(),
                snapshot,
                "osv",
                "snapshot-osv",
                "sha256:" + "a" * 64,
            )
            active = Path(directory) / "active"
            active.symlink_to(snapshot, target_is_directory=True)
            result = subprocess.run(
                [sys.executable, "-m", "src", "vuln-db", "database-info", "--active", str(active)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["state"], "ok")
            self.assertEqual(report["snapshot_id"], "snapshot-osv")
            self.assertEqual(report["source_id"], "osv")
            self.assertEqual(report["advisory_count"], 1)
            self.assertIn("content_digest", report)
            self.assertIn("manifest_sha256", report)
            self.assertNotIn("record_json", result.stdout)
            self.assertNotIn("OSV-V9A-1", result.stdout)

    def test_invalid_active_pointer_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, "-m", "src", "vuln-db", "database-info", "--active", str(Path(directory) / "missing")],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 3)
            report = json.loads(result.stderr)
            self.assertEqual(report["state"], "rejected")


if __name__ == "__main__":
    unittest.main()
