"""RED tests for V5d source-record provenance and explicit merge ledger."""

import unittest

from src.vulnerability.database import VulnerabilityDatabase
from src.vulnerability.fingerprints import source_record_fingerprint


RECORD = {
    "id": "OSV-V5D-1",
    "aliases": ["CVE-2025-1001"],
    "summary": "first revision",
    "affected": [],
}


class TestV5dSourceProvenanceLedger(unittest.TestCase):
    def test_source_record_keeps_stable_identity_and_canonical_revision_digest(self):
        database = VulnerabilityDatabase()
        first = database.record_source_record("osv", "OSV-V5D-1", RECORD, "OSV-V5D-1")
        changed = dict(RECORD, summary="second revision")
        second = database.record_source_record("osv", "OSV-V5D-1", changed, "OSV-V5D-1")
        self.assertEqual(first["fingerprint"], source_record_fingerprint("osv", "OSV-V5D-1"))
        self.assertEqual(first["fingerprint"], second["fingerprint"])
        self.assertNotEqual(first["content_digest"], second["content_digest"])
        self.assertEqual(database.source_record_revision_count("osv", "OSV-V5D-1"), 2)

    def test_source_record_storage_is_idempotent_and_preserves_canonical_json(self):
        database = VulnerabilityDatabase()
        first = database.record_source_record("osv", "OSV-V5D-1", RECORD, "OSV-V5D-1")
        second = database.record_source_record("osv", "OSV-V5D-1", dict(reversed(list(RECORD.items()))), "OSV-V5D-1")
        self.assertEqual(first, second)
        self.assertEqual(database.source_record_revision_count("osv", "OSV-V5D-1"), 1)
        self.assertEqual(database.source_record_history("osv", "OSV-V5D-1")[0]["record"]["id"], "OSV-V5D-1")

    def test_merge_decision_requires_reason_and_provenance_and_does_not_mutate_advisories(self):
        database = VulnerabilityDatabase()
        database.import_osv_records([RECORD, {"id": "OSV-V5D-2", "aliases": [], "affected": []}])
        before = database.advisory_metadata("OSV-V5D-1")
        decision = database.record_merge_decision("merge", ["OSV-V5D-1", "OSV-V5D-2"], "confirmed shared CVE alias", ["osv:OSV-V5D-1", "osv:OSV-V5D-2"])
        self.assertTrue(decision["decision_id"].startswith("sha256:"))
        self.assertEqual(database.merge_decision_count(), 1)
        self.assertEqual(database.advisory_metadata("OSV-V5D-1"), before)
        with self.assertRaises(ValueError):
            database.record_merge_decision("merge", ["OSV-V5D-1", "OSV-V5D-2"], "", ["osv:OSV-V5D-1"])

    def test_invalid_merge_type_or_unknown_advisory_is_rejected(self):
        database = VulnerabilityDatabase()
        database.import_osv_records([RECORD])
        with self.assertRaises(ValueError):
            database.record_merge_decision("heuristic", ["OSV-V5D-1"], "reason", ["osv:OSV-V5D-1"])
        with self.assertRaises(ValueError):
            database.record_merge_decision("split", ["OSV-V5D-1", "missing"], "reason", ["osv:OSV-V5D-1"])


if __name__ == "__main__":
    unittest.main()
