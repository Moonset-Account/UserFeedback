"""RED tests for explicit enrichment quality thresholds."""

import unittest

from src.vulnerability.database import VulnerabilityDatabase
from tests.test_v5m_nvd_enrichment import NVD


class TestV5vQualityThresholds(unittest.TestCase):
    def test_empty_database_is_not_evaluable_and_never_passes(self):
        report = VulnerabilityDatabase().quality_threshold_report(min_advisories=1)
        self.assertEqual(report["quality_status"], "failed")
        self.assertEqual(report["checks"]["min_advisories"]["status"], "not_evaluable")

    def test_required_missing_enrichment_fails_explicitly(self):
        database = VulnerabilityDatabase()
        database.import_osv_records([{"id": "OSV-THRESHOLD-1", "aliases": ["CVE-2025-9601"], "summary": "OSV", "affected": [], "references": []}])
        report = database.quality_threshold_report(required_enrichment_sources=("nvd",))
        self.assertEqual(report["quality_status"], "failed")
        self.assertEqual(report["checks"]["enrichment:nvd"]["status"], "fail")
        self.assertEqual(report["checks"]["enrichment:nvd"]["observed"], 0)

    def test_available_required_enrichment_passes(self):
        database = VulnerabilityDatabase()
        database.import_osv_records([{"id": "OSV-THRESHOLD-2", "aliases": ["CVE-2025-9601"], "summary": "OSV", "affected": [], "references": []}])
        database.import_nvd_json(NVD)
        report = database.quality_threshold_report(required_enrichment_sources=("nvd",))
        self.assertEqual(report["quality_status"], "pass")
        self.assertEqual(report["checks"]["enrichment:nvd"]["status"], "pass")

    def test_thresholds_and_sources_are_validated(self):
        database = VulnerabilityDatabase()
        with self.assertRaises(ValueError):
            database.quality_threshold_report(min_advisories=-1)
        with self.assertRaises(ValueError):
            database.quality_threshold_report(required_enrichment_sources=("unknown",))


if __name__ == "__main__":
    unittest.main()
