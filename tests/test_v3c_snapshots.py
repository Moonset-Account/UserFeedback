"""RED tests for V3c deterministic snapshot activation and quality gates."""

import copy
import unittest

from src.vulnerability.database import SnapshotActivationError, VulnerabilityDatabase


RECORD = {
    "schema_version": "1.4.0",
    "id": "OSV-2025-SNAPSHOT",
    "aliases": ["CVE-2025-2222"],
    "summary": "Synthetic snapshot fixture",
    "affected": [{
        "package": {"ecosystem": "PyPI", "name": "demo"},
        "ranges": [{"type": "ECOSYSTEM", "events": [{"introduced": "0"}, {"fixed": "2.0.0"}]}],
    }],
}


class TestV3cSnapshots(unittest.TestCase):
    def test_same_records_have_same_manifest_digest_regardless_of_import_order(self):
        second = dict(RECORD, id="OSV-2025-SNAPSHOT-SECOND")
        first_db = VulnerabilityDatabase(":memory:")
        first_db.import_osv_records([RECORD, second])
        second_db = VulnerabilityDatabase(":memory:")
        second_db.import_osv_records([second, RECORD])
        first_manifest = first_db.build_snapshot_manifest()
        second_manifest = second_db.build_snapshot_manifest()
        self.assertEqual(first_manifest, second_manifest)
        self.assertTrue(first_manifest["content_digest"].startswith("sha256:"))
        self.assertEqual(first_manifest["advisory_count"], 2)
        self.assertEqual(first_manifest["affected_package_count"], 2)

    def test_valid_staged_snapshot_can_be_activated_and_read_back(self):
        database = VulnerabilityDatabase(":memory:")
        database.import_osv_records([RECORD])
        manifest = database.build_snapshot_manifest()
        database.stage_snapshot("snapshot-2025-01", "sha256:source-fixture", manifest)
        self.assertEqual(database.snapshot_status("snapshot-2025-01")["state"], "staged")
        result = database.activate_snapshot("snapshot-2025-01")
        self.assertEqual(result["state"], "active")
        active = database.active_snapshot()
        self.assertEqual(active["snapshot_id"], "snapshot-2025-01")
        self.assertEqual(active["source_digest"], "sha256:source-fixture")
        self.assertEqual(active["content_digest"], manifest["content_digest"])

    def test_activation_is_blocked_when_manifest_quality_or_digest_differs(self):
        database = VulnerabilityDatabase(":memory:")
        database.import_osv_records([RECORD])
        manifest = database.build_snapshot_manifest()
        tampered = copy.deepcopy(manifest)
        tampered["advisory_count"] = 99
        database.stage_snapshot("snapshot-bad", "sha256:source-fixture", tampered)
        with self.assertRaises(SnapshotActivationError) as raised:
            database.activate_snapshot("snapshot-bad")
        self.assertIn("advisory_count", str(raised.exception))
        self.assertEqual(database.snapshot_status("snapshot-bad")["state"], "staged")

    def test_only_one_snapshot_is_active(self):
        database = VulnerabilityDatabase(":memory:")
        database.import_osv_records([RECORD])
        manifest = database.build_snapshot_manifest()
        database.stage_snapshot("snapshot-one", "sha256:one", manifest)
        database.activate_snapshot("snapshot-one")
        database.stage_snapshot("snapshot-two", "sha256:two", manifest)
        database.activate_snapshot("snapshot-two")
        self.assertEqual(database.active_snapshot()["snapshot_id"], "snapshot-two")
        self.assertEqual(database.snapshot_status("snapshot-one")["state"], "retired")
        self.assertEqual(database.snapshot_status("snapshot-two")["state"], "active")

    def test_invalid_source_digest_cannot_be_staged(self):
        database = VulnerabilityDatabase(":memory:")
        manifest = database.build_snapshot_manifest()
        with self.assertRaises(ValueError):
            database.stage_snapshot("snapshot-invalid", "not-a-digest", manifest)


if __name__ == "__main__":
    unittest.main()
