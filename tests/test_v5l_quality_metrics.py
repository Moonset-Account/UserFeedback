"""RED tests for deterministic local vulnerability database quality metrics."""

import unittest

from src.vulnerability.database import VulnerabilityDatabase


class TestV5lQualityMetrics(unittest.TestCase):
    def test_reports_multisource_coverage_without_claiming_unavailable_enrichment(self):
        database = VulnerabilityDatabase()
        database.import_osv_records([{
            "id": "OSV-METRICS-1", "aliases": ["CVE-2025-9501"], "summary": "osv",
            "severity": [{"type": "OSV", "score": "high"}],
            "affected": [{"package": {"ecosystem": "PyPI", "name": "fixture"}, "ranges": [{"type": "ECOSYSTEM", "events": [{"introduced": "0"}, {"fixed": "2.0"}]}]}],
            "references": [],
        }])
        database.import_ghsa_json({
            "ghsa_id": "GHSA-abcd-efgh-ijkl", "cve_id": "CVE-2025-9501", "summary": "ghsa",
            "description": "details", "severity": "high", "vulnerabilities": [], "references": [],
        })
        metrics = database.quality_metrics_report()
        self.assertEqual(metrics["quality_status"], "pass")
        self.assertEqual(metrics["advisory_count"], 2)
        self.assertEqual(metrics["advisory_count_by_source"], {"github-advisory": 1, "osv": 1})
        self.assertEqual(metrics["unique_cve_count"], 1)
        self.assertEqual(metrics["affected_range_count"], 1)
        self.assertEqual(metrics["range_with_fixed_event_count"], 1)
        self.assertEqual(metrics["unavailable_enrichment"], {"cpe": 0, "cwe": 0, "epss": 0, "kev": 0})
        self.assertTrue(metrics["content_digest"].startswith("sha256:"))

    def test_empty_database_fails_minimal_quality_gate(self):
        metrics = VulnerabilityDatabase().quality_metrics_report()
        self.assertEqual(metrics["quality_status"], "failed")
        self.assertIn("no_advisories", metrics["quality_warnings"])


if __name__ == "__main__":
    unittest.main()
