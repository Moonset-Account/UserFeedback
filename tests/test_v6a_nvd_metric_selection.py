"""RED tests for deterministic NVD CVSS metric presentation selection."""

import copy
import unittest

from src.vulnerability.database import VulnerabilityDatabase
from src.vulnerability.sources.nvd import parse_nvd_cve


NVD_MULTI = {
    "cve": {
        "id": "CVE-2026-9601",
        "published": "2026-01-01T00:00:00.000",
        "lastModified": "2026-01-02T00:00:00.000",
        "vulnStatus": "Analyzed",
        "descriptions": [{"lang": "en", "value": "metric selection fixture"}],
        "metrics": {
            "cvssMetricV31": [
                {"source": "secondary.example", "type": "Secondary", "cvssData": {"version": "3.1", "baseScore": 10.0}},
                {"source": "nvd@nist.gov", "type": "Primary", "cvssData": {"version": "3.1", "baseScore": 7.5}},
            ],
            "cvssMetricV40": [
                {"source": "nvd@nist.gov", "type": "Primary", "cvssData": {"version": "4.0", "baseScore": 6.8}},
            ],
        },
        "weaknesses": [],
        "configurations": [],
    }
}


class TestV6aNvdMetricSelection(unittest.TestCase):
    def test_parser_preserves_all_cvss_metrics(self):
        parsed = parse_nvd_cve(NVD_MULTI)
        self.assertEqual(len(parsed["cvss"]), 3)

    def test_normalized_report_selects_highest_version_then_primary(self):
        database = VulnerabilityDatabase()
        database.import_osv_records([{"id": "OSV-V6A-1", "aliases": ["CVE-2026-9601"], "summary": "fixture", "affected": [], "references": []}])
        database.import_nvd_json(NVD_MULTI)
        report = database.nvd_normalized_report("CVE-2026-9601")
        self.assertEqual(len(report["cvss"]), 3)
        self.assertEqual(report["preferred_cvss"]["version"], "40")
        self.assertEqual(report["preferred_cvss"]["source"], "nvd")
        self.assertEqual(report["preferred_cvss"]["type"], "Primary")

    def test_cvss_revision_does_not_change_advisory_fingerprint(self):
        database = VulnerabilityDatabase()
        database.import_osv_records([{"id": "OSV-V6A-1", "aliases": ["CVE-2026-9601"], "summary": "fixture", "affected": [], "references": []}])
        database.import_nvd_json(NVD_MULTI)
        before = database.build_snapshot_manifest()["content_digest"]
        changed = copy.deepcopy(NVD_MULTI)
        changed["cve"]["metrics"]["cvssMetricV40"][0]["cvssData"]["baseScore"] = 9.9
        changed["cve"]["lastModified"] = "2026-01-03T00:00:00.000"
        database.import_nvd_json(changed)
        self.assertEqual(before, database.build_snapshot_manifest()["content_digest"])


if __name__ == "__main__":
    unittest.main()
