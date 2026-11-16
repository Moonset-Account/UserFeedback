"""RED tests for deterministic exact cross-source reconciliation."""

import unittest

from src.vulnerability.database import VulnerabilityDatabase


OSV = {"id": "OSV-RECON-1", "aliases": ["CVE-2025-9001"], "summary": "fixture", "affected": []}
GHSA = {
    "ghsa_id": "GHSA-RECN-ABCD-EFGH",
    "cve_id": "CVE-2025-9001",
    "summary": "fixture",
    "description": "fixture",
    "identifiers": [{"value": "GHSA-RECN-ABCD-EFGH", "type": "GHSA"}],
    "vulnerabilities": [],
    "references": [],
}


class TestV5gCrossSourceReconciliation(unittest.TestCase):
    def test_exact_alias_creates_cross_source_ambiguous_group_without_merging(self):
        database = VulnerabilityDatabase()
        database.import_osv_records([OSV])
        database.import_ghsa_json(GHSA)
        report = database.cross_source_reconciliation_report()
        self.assertEqual(report["cross_source_group_count"], 1)
        group = report["groups"][0]
        self.assertEqual(group["source_scope"], "cross-source")
        self.assertTrue(group["ambiguous"])
        self.assertEqual(group["evidence_aliases"], ["CVE-2025-9001"])
        self.assertEqual(database.advisory_count(), 2)

    def test_report_is_deterministic_and_includes_source_revision_counts(self):
        database = VulnerabilityDatabase()
        database.import_osv_records([OSV])
        database.import_ghsa_json(GHSA)
        first = database.cross_source_reconciliation_report()
        second = database.cross_source_reconciliation_report()
        self.assertEqual(first, second)
        self.assertTrue(first["content_digest"].startswith("sha256:"))
        self.assertEqual(first["groups"][0]["source_record_revisions"], {"github-advisory": 1, "osv": 1})

    def test_unrelated_advisory_is_single_source_and_not_in_cross_source_count(self):
        database = VulnerabilityDatabase()
        database.import_osv_records([OSV, {"id": "OSV-RECON-2", "aliases": [], "summary": "other", "affected": []}])
        report = database.cross_source_reconciliation_report()
        self.assertEqual(report["cross_source_group_count"], 0)
        self.assertEqual(report["groups"][-1]["source_scope"], "single-source")


if __name__ == "__main__":
    unittest.main()
