"""RED tests for truthful enrichment coverage reporting."""

import unittest

from src.vulnerability.database import VulnerabilityDatabase
from tests.test_v5m_nvd_enrichment import NVD


class TestV5uEnrichmentCoverage(unittest.TestCase):
    def test_coverage_distinguishes_available_unavailable_and_not_requested(self):
        database = VulnerabilityDatabase()
        database.import_osv_records([{"id": "OSV-COVERAGE-1", "aliases": ["CVE-2025-9601"], "summary": "OSV", "affected": [], "references": []}])
        database.import_nvd_json(NVD)
        report = database.enrichment_coverage_report("CVE-2025-9601", requested_sources=("nvd", "kev"))
        self.assertEqual(report["sources"]["nvd"]["status"], "available")
        self.assertEqual(report["sources"]["kev"]["status"], "unavailable")
        self.assertEqual(report["counts"], {"available": 1, "unavailable": 1, "not_requested": 0})

    def test_coverage_marks_unrequested_source_without_calling_it_missing(self):
        database = VulnerabilityDatabase()
        report = database.enrichment_coverage_report("CVE-2025-9601", requested_sources=("nvd",))
        self.assertEqual(report["sources"]["kev"]["status"], "not_requested")
        self.assertEqual(report["counts"]["not_requested"], 1)

    def test_coverage_rejects_unknown_source(self):
        with self.assertRaises(ValueError):
            VulnerabilityDatabase().enrichment_coverage_report("CVE-2025-9601", requested_sources=("unknown",))


if __name__ == "__main__":
    unittest.main()
