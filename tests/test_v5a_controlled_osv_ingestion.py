"""RED tests for V5a controlled local OSV feed ingestion."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from src.vulnerability.database import VulnerabilityDatabase
from src.vulnerability.ingestion import ingest_osv_file


RECORD = {
    "id": "OSV-2025-INGEST",
    "aliases": ["CVE-2025-5555"],
    "affected": [{"package": {"ecosystem": "PyPI", "name": "requests"}, "ranges": [{"type": "ECOSYSTEM", "events": [{"introduced": "0"}, {"fixed": "2.32.0"}]}]}],
}


class TestV5aControlledOSVIngestion(unittest.TestCase):
    def test_local_feed_stages_with_sha256_provenance_without_implicit_activation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); feed = root / "osv.json"
            feed.write_text(json.dumps({"vulns": [RECORD]}), encoding="utf-8")
            db = VulnerabilityDatabase(str(root / "vulnerability.sqlite"))
            report = ingest_osv_file(str(feed), db, "feed-1", "osv-fixture", activate=False)
            self.assertEqual(report.state, "staged")
            self.assertFalse(report.activated)
            self.assertEqual(report.records_seen, 1)
            self.assertEqual(report.advisories_imported, 1)
            self.assertTrue(report.source_digest.startswith("sha256:"))
            self.assertEqual(db.active_snapshot(), None)
            self.assertEqual(db.snapshot_status("feed-1")["state"], "staged")

    def test_activate_requires_explicit_flag_and_returns_active_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); feed = root / "osv.json"
            feed.write_text(json.dumps([RECORD]), encoding="utf-8")
            db = VulnerabilityDatabase(str(root / "vulnerability.sqlite"))
            report = ingest_osv_file(str(feed), db, "feed-2", "local-osv", activate=True)
            self.assertEqual(report.state, "active")
            self.assertTrue(report.activated)
            active = db.active_snapshot()
            self.assertEqual(active["snapshot_id"], "feed-2")
            self.assertEqual(active["manifest"]["source_id"], "local-osv")
            self.assertEqual(active["source_digest"], report.source_digest)

    def test_invalid_record_rejects_entire_feed_and_does_not_stage_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); feed = root / "osv.json"
            feed.write_text(json.dumps({"vulns": [RECORD, {"affected": []}]}), encoding="utf-8")
            db = VulnerabilityDatabase(str(root / "vulnerability.sqlite"))
            report = ingest_osv_file(str(feed), db, "feed-bad", "fixture", activate=True)
            self.assertEqual(report.state, "rejected")
            self.assertFalse(report.activated)
            self.assertEqual(report.advisories_imported, 0)
            self.assertTrue(report.errors)
            with self.assertRaises(KeyError):
                db.snapshot_status("feed-bad")
            self.assertEqual(db.advisory_count(), 0)

    def test_malformed_or_oversized_feed_is_rejected_before_database_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); feed = root / "bad.json"
            feed.write_text("not json", encoding="utf-8")
            db = VulnerabilityDatabase(str(root / "vulnerability.sqlite"))
            report = ingest_osv_file(str(feed), db, "bad", "fixture")
            self.assertEqual(report.state, "rejected")
            self.assertEqual(db.advisory_count(), 0)

    def test_cli_requires_explicit_local_input_and_activation_flag(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); feed = root / "osv.json"; db = root / "vulnerability.sqlite"
            feed.write_text(json.dumps({"vulns": [RECORD]}), encoding="utf-8")
            first = subprocess.run([sys.executable, "-m", "src", "osv-import", "--input", str(feed), "--db", str(db), "--snapshot-id", "cli-1", "--source-id", "fixture"], capture_output=True, text=True)
            self.assertEqual(first.returncode, 0)
            self.assertEqual(json.loads(first.stdout)["state"], "staged")
            second = subprocess.run([sys.executable, "-m", "src", "osv-import", "--input", str(feed), "--db", str(db), "--snapshot-id", "cli-2", "--source-id", "fixture", "--activate"], capture_output=True, text=True)
            self.assertEqual(second.returncode, 0)
            self.assertEqual(json.loads(second.stdout)["state"], "active")


if __name__ == "__main__":
    unittest.main()
