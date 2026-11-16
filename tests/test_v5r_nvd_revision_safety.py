"""RED regressions for strict NVD validation and revision-safe normalization."""

import copy
import json
import unittest

from src.vulnerability.database import VulnerabilityDatabase
from src.vulnerability.sources.nvd import parse_nvd_cve
from tests.test_v5m_nvd_enrichment import NVD


class TestV5rNvdRevisionSafety(unittest.TestCase):
    def test_nested_nvd_payload_types_fail_closed(self):
        malformed = {
            "cve": {
                "id": "CVE-2025-1234",
                "descriptions": "bad",
                "metrics": {"cvssMetricV31": [{"cvssData": "bad"}]},
                "weaknesses": "bad",
                "configurations": "bad",
            }
        }
        with self.assertRaises(ValueError):
            parse_nvd_cve(malformed)

    def test_invalid_json_returns_import_error_stats(self):
        stats = VulnerabilityDatabase().import_nvd_json("{")
        self.assertEqual(stats.records_seen, 0)
        self.assertEqual(stats.advisories_imported, 0)
        self.assertTrue(stats.errors)

    def test_normalized_report_selects_one_nvd_revision(self):
        database = VulnerabilityDatabase()
        database.import_osv_records([{"id": "OSV-V5R-1", "aliases": ["CVE-2025-9601"], "summary": "x", "affected": [], "references": []}])
        old = copy.deepcopy(NVD)
        new = copy.deepcopy(NVD)
        old["cve"]["lastModified"] = "2025-01-01T00:00:00.000"
        new["cve"]["lastModified"] = "2025-02-01T00:00:00.000"
        old["cve"]["metrics"]["cvssMetricV31"][0]["cvssData"]["baseScore"] = 1.0
        new["cve"]["metrics"]["cvssMetricV31"][0]["cvssData"]["baseScore"] = 9.9
        database.import_nvd_json(old)
        database.import_nvd_json(new)
        report = database.nvd_normalized_report("CVE-2025-9601")
        self.assertEqual(report["revision_digest"], database.nvd_enrichment("CVE-2025-9601")["content_digest"])
        self.assertEqual(len(report["cvss"]), 1)
        self.assertEqual(report["cvss"][0]["data"]["baseScore"], 9.9)
        self.assertEqual(report["revision_modified"], "2025-02-01T00:00:00.000")


if __name__ == "__main__":
    unittest.main()
