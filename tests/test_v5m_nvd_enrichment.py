"""RED tests for strict offline NVD enrichment."""

import unittest

from src.vulnerability.database import VulnerabilityDatabase
from src.vulnerability.sources.nvd import parse_nvd_cve


NVD = {
    "cve": {
        "id": "CVE-2025-9601", "published": "2025-01-01T00:00:00.000", "lastModified": "2025-01-02T00:00:00.000", "vulnStatus": "Analyzed",
        "descriptions": [{"lang": "en", "value": "NVD enrichment description"}],
        "metrics": {"cvssMetricV31": [{"source": "nvd@nist.gov", "type": "Primary", "cvssData": {"version": "3.1", "vectorString": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H", "baseScore": 9.8}}]},
        "weaknesses": [{"description": [{"lang": "en", "value": "CWE-89"}]}],
        "configurations": [{"nodes": [{"cpeMatch": [{"vulnerable": True, "criteria": "cpe:2.3:a:vendor:product:*:*:*:*:*:*:*:*", "versionEndExcluding": "2.0"}]}]}],
    }
}


class TestV5mNvdEnrichment(unittest.TestCase):
    def test_parser_preserves_cvss_cwe_cpe_and_source_record(self):
        parsed = parse_nvd_cve(NVD)
        self.assertEqual(parsed["id"], "CVE-2025-9601")
        self.assertEqual(parsed["cvss"][0]["data"]["baseScore"], 9.8)
        self.assertEqual(parsed["weaknesses"][0]["value"], "CWE-89")
        self.assertEqual(parsed["cpe_matches"][0]["versionEndExcluding"], "2.0")
        self.assertIn("_source_record", parsed)

    def test_import_stores_nvd_enrichment_without_overwriting_osv_advisory(self):
        database = VulnerabilityDatabase()
        database.import_osv_records([{"id": "OSV-NVD-1", "aliases": ["CVE-2025-9601"], "summary": "OSV summary", "affected": [], "references": []}])
        stats = database.import_nvd_json(NVD)
        self.assertEqual(stats.advisories_imported, 1)
        self.assertEqual(database.advisory_metadata("OSV-NVD-1")["summary"], "OSV summary")
        enrichment = database.nvd_enrichment("CVE-2025-9601")
        self.assertEqual(enrichment["cvss"][0]["data"]["baseScore"], 9.8)
        self.assertEqual(enrichment["advisory_id"], "OSV-NVD-1")

    def test_ambiguous_or_unknown_cve_is_rejected_without_partial_import(self):
        database = VulnerabilityDatabase()
        stats = database.import_nvd_json(NVD)
        self.assertEqual(stats.advisories_imported, 0)
        self.assertEqual(stats.errors, ("record 1: CVE has no exact advisory match",))


if __name__ == "__main__":
    unittest.main()
