"""RED tests for normalized NVD enrichment provenance."""

import unittest

from src.vulnerability.database import VulnerabilityDatabase
from tests.test_v5m_nvd_enrichment import NVD


class TestV5nNormalizedNvd(unittest.TestCase):
    def test_normalized_readback_separates_cvss_cwe_cpe_with_source(self):
        database = VulnerabilityDatabase()
        database.import_osv_records([{"id": "OSV-NORM-1", "aliases": ["CVE-2025-9601"], "summary": "OSV", "affected": [], "references": []}])
        database.import_nvd_json(NVD)
        report = database.nvd_normalized_report("CVE-2025-9601")
        self.assertEqual(report["cvss"][0]["source"], "nvd")
        self.assertEqual(report["cvss"][0]["data"]["baseScore"], 9.8)
        self.assertEqual(report["weaknesses"][0]["source"], "nvd")
        self.assertEqual(report["cpe_matches"][0]["source"], "nvd")
        self.assertTrue(report["content_digest"].startswith("sha256:"))

    def test_normalized_tables_are_empty_when_raw_nvd_import_is_rejected(self):
        database = VulnerabilityDatabase()
        database.import_nvd_json(NVD)
        with self.assertRaises(KeyError):
            database.nvd_normalized_report("CVE-2025-9601")


if __name__ == "__main__":
    unittest.main()
