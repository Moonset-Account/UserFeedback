"""RED tests for V5c exact advisory alias indexing and conflict handling."""

import unittest

from src.vulnerability.database import VulnerabilityDatabase


RECORD_A = {
    "id": "OSV-V5C-A",
    "aliases": ["CVE-2024-0001", "GHSA-aaaa-bbbb-cccc"],
    "affected": [{"package": {"ecosystem": "PyPI", "name": "demo", "purl": "pkg:pypi/demo@1.0.0"}}],
}


class TestV5cAdvisoryAliasIndex(unittest.TestCase):
    def test_native_id_and_explicit_aliases_resolve_exactly(self):
        database = VulnerabilityDatabase()
        database.import_osv_records([RECORD_A])
        self.assertEqual(database.lookup_advisory("OSV-V5C-A")["status"], "exact")
        self.assertEqual(database.lookup_advisory("cve-2024-0001")["advisory_id"], "OSV-V5C-A")
        self.assertEqual(database.lookup_advisory("GHSA-aaaa-bbbb-cccc")["advisory_id"], "OSV-V5C-A")
        self.assertEqual(database.lookup_advisory("CVE-2024-9999")["status"], "not-found")

    def test_reimport_is_idempotent_for_aliases(self):
        database = VulnerabilityDatabase()
        database.import_osv_records([RECORD_A, RECORD_A])
        self.assertEqual(database.alias_count(), 3)
        self.assertEqual(database.lookup_advisory("CVE-2024-0001")["status"], "exact")

    def test_conflicting_alias_is_ambiguous_and_never_silently_merged(self):
        database = VulnerabilityDatabase()
        other = {"id": "OSV-V5C-B", "aliases": ["CVE-2024-0001"], "affected": []}
        database.import_osv_records([RECORD_A, other])
        result = database.lookup_advisory("CVE-2024-0001")
        self.assertEqual(result["status"], "ambiguous")
        self.assertEqual(result["advisory_ids"], ["OSV-V5C-A", "OSV-V5C-B"])
        self.assertEqual(database.alias_conflict_count(), 1)

    def test_alias_index_does_not_change_advisory_identity_or_source_data(self):
        database = VulnerabilityDatabase()
        database.import_osv_records([RECORD_A])
        before = database.advisory_metadata("OSV-V5C-A")
        database.correlate_aliases()
        after = database.advisory_metadata("OSV-V5C-A")
        self.assertEqual(before["id"], after["id"])
        self.assertEqual(before["aliases"], after["aliases"])


if __name__ == "__main__":
    unittest.main()
