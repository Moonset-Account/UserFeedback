"""RED tests for V8h metadata-only snapshot reconciliation."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from src.vulnerability.updater import (
    build_reconciliation_report,
    promote_versioned_snapshot,
    stage_versioned_snapshot,
)


RECORD = {"id": "OSV-2025-V8H", "affected": [{"package": {"ecosystem": "PyPI", "name": "v8h-demo"}}]}


class TestV8hReconciliationReport(unittest.TestCase):
    def _make(self, root, name, suffix):
        path = root / name
        stage_versioned_snapshot(
            json.dumps([dict(RECORD, id=f"{RECORD['id']}-{name}")]).encode(),
            path,
            "osv",
            name,
            "sha256:" + suffix * 64,
        )
        return path

    def test_report_is_deterministic_and_describes_active_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            active_snapshot = self._make(root, "snapshot-active", "a")
            self._make(root, "snapshot-older", "b")
            active = root / "active"
            promote_versioned_snapshot(active_snapshot, active)

            first = build_reconciliation_report(root, active)
            second = build_reconciliation_report(root, active)

            self.assertEqual(first, second)
            self.assertEqual(first["schema"], "coderisktools.vulnerability.reconciliation")
            self.assertEqual(first["state"], "ok")
            self.assertEqual(first["active_snapshot_id"], "snapshot-active")
            self.assertEqual(first["snapshot_count"], 2)
            self.assertEqual(first["source_counts"], {"osv": 2})
            self.assertEqual(first["issues"], [])
            self.assertEqual(first["report_sha256"], build_reconciliation_report(root, active)["report_sha256"])

    def test_tampered_snapshot_is_reported_without_activation_or_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            active_snapshot = self._make(root, "snapshot-active", "c")
            tampered = self._make(root, "snapshot-tampered", "d")
            active = root / "active"
            promote_versioned_snapshot(active_snapshot, active)
            manifest = tampered / "manifest.json"
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            payload["content_digest"] = "sha256:" + "0" * 64
            manifest.write_text(json.dumps(payload), encoding="utf-8")

            report = build_reconciliation_report(root, active)

            self.assertEqual(report["state"], "reconciliation_failed")
            self.assertEqual(report["active_snapshot_id"], "snapshot-active")
            self.assertEqual(report["valid_snapshot_count"], 1)
            self.assertEqual(report["invalid_snapshot_count"], 1)
            self.assertEqual(report["issues"][0]["path"], "snapshot-tampered")
            self.assertTrue(active.is_symlink())
            self.assertEqual(active.resolve(), active_snapshot.resolve())

    def test_cli_reconcile_is_explicit_and_metadata_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            active_snapshot = self._make(root, "snapshot-active", "e")
            active = root / "active"
            promote_versioned_snapshot(active_snapshot, active)
            output = subprocess.run(
                [sys.executable, "-m", "src", "vuln-db", "reconcile", "--root", str(root), "--active", str(active)],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(output.returncode, 0, output.stderr)
            report = json.loads(output.stdout)
            self.assertEqual(report["active_snapshot_id"], "snapshot-active")
            self.assertNotIn("record_json", output.stdout)
            self.assertEqual(active.resolve(), active_snapshot.resolve())


if __name__ == "__main__":
    unittest.main()
