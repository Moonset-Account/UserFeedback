"""RED tests for exact cross-source conflict diagnostics."""

import unittest

from src.vulnerability.database import VulnerabilityDatabase


class TestV5hCrossSourceConflicts(unittest.TestCase):
    def test_reports_summary_and_severity_conflicts_without_resolving_them(self):
        database = VulnerabilityDatabase()
        database.import_osv_records([{"id": "OSV-CONFLICT-1", "aliases": ["CVE-2025-9101"], "summary": "OSV wording", "affected": [], "severity": [{"type": "OSV", "score": "low"}], "references": []}])
        database.import_cve_v5_json({
            "dataType": "CVE_RECORD", "dataVersion": "5.1",
            "cveMetadata": {"cveId": "CVE-2025-9101", "state": "PUBLISHED"},
            "containers": {"cna": {"descriptions": [{"lang": "en", "value": "CVE wording"}], "references": []}},
        })
        report = database.cross_source_conflict_report()
        self.assertEqual(report["conflict_group_count"], 1)
        conflict_types = {item["conflict_type"] for item in report["groups"][0]["conflicts"]}
        self.assertEqual(conflict_types, {"summary_details", "severity"})
        self.assertEqual(database.advisory_count(), 2)

    def test_identical_source_signatures_produce_no_conflict(self):
        database = VulnerabilityDatabase()
        record = {"id": "OSV-CONFLICT-2", "aliases": ["CVE-2025-9102"], "summary": "same", "affected": [], "severity": [], "references": []}
        database.import_osv_records([record])
        database.import_osv_records([{**record, "id": "OSV-CONFLICT-3"}])
        report = database.cross_source_conflict_report()
        self.assertEqual(report["conflict_group_count"], 0)

    def test_conflict_report_is_deterministic_and_digest_backed(self):
        database = VulnerabilityDatabase()
        database.import_osv_records([{"id": "OSV-CONFLICT-4", "aliases": [], "summary": "solo", "affected": [], "references": []}])
        first = database.cross_source_conflict_report()
        second = database.cross_source_conflict_report()
        self.assertEqual(first, second)
        self.assertTrue(first["content_digest"].startswith("sha256:"))


if __name__ == "__main__":
    unittest.main()
