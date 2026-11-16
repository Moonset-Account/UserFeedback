"""RED tests for V8d isolated versioned snapshot staging."""

import json
import tempfile
import unittest
from pathlib import Path

from src.vulnerability.updater import stage_versioned_snapshot, verify_versioned_snapshot


RECORD = {
    "id": "OSV-2025-V8D",
    "aliases": ["CVE-2025-8888"],
    "summary": "Synthetic V8d snapshot",
    "affected": [{
        "package": {"ecosystem": "PyPI", "name": "v8d-demo"},
        "ranges": [{"type": "ECOSYSTEM", "events": [{"introduced": "0"}, {"fixed": "2.0.0"}]}],
    }],
}


class TestV8dVersionedSnapshotStaging(unittest.TestCase):
    def test_stages_isolated_sqlite_snapshot_and_deterministic_manifest(self):
        payload = json.dumps([RECORD], sort_keys=True).encode()
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "snapshot-2025-01"
            first = stage_versioned_snapshot(payload, target, "osv", "snapshot-2025-01", "sha256:" + "a" * 64)
            first_manifest = (target / "manifest.json").read_bytes()
            second_target = Path(directory) / "snapshot-2025-01-copy"
            second = stage_versioned_snapshot(payload, second_target, "osv", "snapshot-2025-01", "sha256:" + "a" * 64)
            self.assertEqual(first, second)
            self.assertEqual(first_manifest, (second_target / "manifest.json").read_bytes())
            self.assertEqual(verify_versioned_snapshot(target), first)
            self.assertEqual(first["state"], "staged")
            self.assertFalse(first["activated"])

    def test_rejects_invalid_import_atomically_and_does_not_create_target(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "bad-snapshot"
            with self.assertRaises(ValueError):
                stage_versioned_snapshot(b"[{bad", target, "osv", "snapshot-bad", "sha256:" + "b" * 64)
            self.assertFalse(target.exists())

    def test_does_not_mutate_existing_target_or_activate_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "snapshot-existing"
            target.mkdir()
            marker = target / "marker"
            marker.write_text("keep", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                stage_versioned_snapshot(json.dumps([RECORD]).encode(), target, "osv", "snapshot-existing", "sha256:" + "c" * 64)
            self.assertEqual(marker.read_text(encoding="utf-8"), "keep")


if __name__ == "__main__":
    unittest.main()
