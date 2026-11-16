"""RED regressions for non-destructive advisory reimport."""

import unittest

from src.vulnerability.database import VulnerabilityDatabase
from tests.test_v5m_nvd_enrichment import NVD
from tests.test_v5p_kev import KEV


class TestV5qReimportPreservation(unittest.TestCase):
    def test_advisory_reimport_preserves_nvd_and_kev_enrichment(self):
        database = VulnerabilityDatabase()
        original = {"id": "OSV-REIMPORT-1", "aliases": ["CVE-2025-9601"], "summary": "before", "affected": [], "references": []}
        database.import_osv_records([original])
        database.import_nvd_json(NVD)
        database.import_kev_json({"vulnerabilities": [KEV]})
        updated = {**original, "summary": "after"}
        database.import_osv_records([updated])
        self.assertEqual(database.advisory_metadata("OSV-REIMPORT-1")["summary"], "after")
        self.assertEqual(database.nvd_enrichment("CVE-2025-9601")["advisory_id"], "OSV-REIMPORT-1")
        self.assertEqual(database.kev_record("CVE-2025-9601")["advisory_id"], "OSV-REIMPORT-1")
        self.assertEqual(database.connection.execute("SELECT COUNT(*) FROM nvd_cvss").fetchone()[0], 1)
        self.assertEqual(database.connection.execute("SELECT COUNT(*) FROM kev_records").fetchone()[0], 1)

    def test_reimport_preserves_active_relation(self):
        database = VulnerabilityDatabase()
        records = [
            {"id": "OSV-REL-A", "aliases": ["CVE-2025-9701"], "summary": "a", "affected": [], "references": []},
            {"id": "OSV-REL-B", "aliases": ["CVE-2025-9701"], "summary": "b", "affected": [], "references": []},
        ]
        database.import_osv_records(records)
        decision = database.record_merge_decision("merge", ["OSV-REL-A", "OSV-REL-B"], "fixture", ["local"])
        self.assertEqual(database.apply_merge_decision(decision["decision_id"])["status"], "applied")
        database.import_osv_records([{**records[0], "summary": "a2"}])
        self.assertEqual(database.advisory_relation_count(active_only=True), 1)

    def test_duplicate_nvd_and_kev_imports_report_zero_new_records(self):
        database = VulnerabilityDatabase()
        database.import_osv_records([{"id": "OSV-DUP-1", "aliases": ["CVE-2025-9601"], "summary": "x", "affected": [], "references": []}])
        self.assertEqual(database.import_nvd_json(NVD).advisories_imported, 1)
        self.assertEqual(database.import_nvd_json(NVD).advisories_imported, 0)
        self.assertEqual(database.import_kev_json({"vulnerabilities": [KEV]}).advisories_imported, 1)
        self.assertEqual(database.import_kev_json({"vulnerabilities": [KEV]}).advisories_imported, 0)


if __name__ == "__main__":
    unittest.main()
