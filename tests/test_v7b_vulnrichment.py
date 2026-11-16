"""RED tests for offline CISA Vulnrichment import and SSVC readback."""

import unittest

from src.vulnerability.database import VulnerabilityDatabase
from src.vulnerability.sources.vulnrichment import parse_vulnrichment_record

VULN = {
    "cveMetadata": {"cveId": "CVE-2026-9702"},
    "containers": {
        "cna": {"title": "CNA data must remain separate"},
        "adp": [{
            "providerMetadata": {"shortName": "CISA-ADP", "orgId": "cisa"},
            "metrics": [{"other": {"type": "ssvc", "content": {"exploitation": "none", "automatable": "no", "technical_impact": "partial"}}}],
            "title": "CISA enrichment",
            "references": [{"url": "https://example.invalid/advisory"}],
        }],
    },
}
OSV = {"id": "OSV-VULN-1", "aliases": ["CVE-2026-9702"], "summary": "Vulnrichment", "affected": [], "references": []}


class TestV7bVulnrichment(unittest.TestCase):
    def test_parser_selects_cisa_adp_and_preserves_ssvc(self):
        parsed = parse_vulnrichment_record(VULN)
        self.assertEqual(parsed["cve_id"], "CVE-2026-9702")
        self.assertEqual(parsed["cisa_adp"][0]["provider"]["shortName"], "CISA-ADP")
        self.assertEqual(parsed["cisa_adp"][0]["ssvc"][0]["technical_impact"], "partial")

    def test_import_requires_exact_advisory_and_exposes_additive_report(self):
        database = VulnerabilityDatabase()
        database.import_osv_records([OSV])
        stats = database.import_vulnrichment_json(VULN)
        self.assertEqual(stats.advisories_imported, 1)
        record = database.vulnrichment_record("CVE-2026-9702")
        self.assertEqual(record["advisory_id"], "OSV-VULN-1")
        self.assertEqual(record["source"], "cisa-vulnrichment")
        report = database.exploitation_intelligence_report("CVE-2026-9702")
        self.assertEqual(report["vulnrichment"]["ssvc"][0]["exploitation"], "none")
        self.assertEqual(report["vulnrichment"]["ssvc"][0]["decision"], "not_evaluable")
        self.assertTrue(report["content_digest"].startswith("sha256:"))

    def test_parser_rejects_missing_cisa_container_or_malformed_ssvc(self):
        missing = {"cveMetadata": {"cveId": "CVE-2026-9702"}, "containers": {"adp": []}}
        with self.assertRaises(ValueError):
            parse_vulnrichment_record(missing)
        malformed = dict(VULN)
        malformed["containers"] = {"adp": [{"providerMetadata": {"shortName": "CISA-ADP"}, "metrics": [{"other": {"type": "ssvc", "content": []}}]}]}
        with self.assertRaises(ValueError):
            parse_vulnrichment_record(malformed)

    def test_unknown_cve_is_rejected_without_record(self):
        database = VulnerabilityDatabase()
        stats = database.import_vulnrichment_json(VULN)
        self.assertEqual(stats.advisories_imported, 0)
        with self.assertRaises(KeyError):
            database.vulnrichment_record("CVE-2026-9702")


if __name__ == "__main__":
    unittest.main()
