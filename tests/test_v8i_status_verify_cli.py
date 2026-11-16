"""RED tests for V8i read-only snapshot status and verification CLI."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from src.vulnerability.updater import promote_versioned_snapshot, stage_versioned_snapshot


class TestV8iStatusVerifyCLI(unittest.TestCase):
    def _make_snapshot(self, root: Path, name: str = "snapshot-v8i") -> Path:
        snapshot = root / name
        stage_versioned_snapshot(
            json.dumps([{"id": "OSV-V8I-SMOKE", "affected": []}]).encode(),
            snapshot,
            "osv",
            name,
            "sha256:" + "f" * 64,
        )
        return snapshot

    def test_verify_command_returns_verified_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = self._make_snapshot(Path(directory))
            result = subprocess.run(
                [sys.executable, "-m", "src", "vuln-db", "verify", "--snapshot", str(snapshot)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["state"], "staged")
            self.assertEqual(report["snapshot_id"], "snapshot-v8i")
            self.assertFalse(report["activated"])
            self.assertNotIn("record_json", result.stdout)

    def test_status_command_reports_active_storage_without_mutating_pointer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = self._make_snapshot(root)
            active = root / "active"
            promote_versioned_snapshot(snapshot, active)
            before = active.resolve()
            result = subprocess.run(
                [sys.executable, "-m", "src", "vuln-db", "status", "--root", str(root), "--active", str(active)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["state"], "ok")
            self.assertEqual(report["active_snapshot_id"], "snapshot-v8i")
            self.assertEqual(report["valid_snapshot_count"], 1)
            self.assertEqual(active.resolve(), before)

    def test_verify_rejects_tampered_snapshot_without_repairing_it(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = self._make_snapshot(Path(directory))
            manifest = snapshot / "manifest.json"
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            payload["content_digest"] = "sha256:" + "0" * 64
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, "-m", "src", "vuln-db", "verify", "--snapshot", str(snapshot)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 3)
            self.assertIn("snapshot", result.stderr)
            self.assertEqual(json.loads(manifest.read_text(encoding="utf-8"))["content_digest"], "sha256:" + "0" * 64)


if __name__ == "__main__":
    unittest.main()
