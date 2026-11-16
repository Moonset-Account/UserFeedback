"""RED tests for canonical CISA KEV ransomware signal projection."""

import unittest

from src.vulnerability.sources.ransomware import normalize_ransomware_signal
from src.vulnerability.database import VulnerabilityDatabase


class TestV7dRansomwareSignal(unittest.TestCase):
    def test_known_values_emit_source_listed_campaign_signal(self):
        for value in (True, "Known", "Yes"):
            result = normalize_ransomware_signal(value)
            self.assertEqual(result["status"], "known")
            self.assertTrue(result["known"])
            self.assertEqual(result["action_signal"], "ransomware-campaign-known")

    def test_unknown_and_not_known_remain_distinct(self):
        self.assertEqual(normalize_ransomware_signal("Unknown")["status"], "unknown")
        self.assertEqual(normalize_ransomware_signal("No")["status"], "not-known")

    def test_missing_signal_is_not_listed_not_negative(self):
        result = normalize_ransomware_signal(None)
        self.assertEqual(result["status"], "not-listed")
        self.assertFalse(result["known"])
        self.assertEqual(result["confidence"], "not-listed")

    def test_report_projects_imported_kev_signal(self):
        database = VulnerabilityDatabase()
        database.import_osv_records([{"id": "OSV-RANSOM-1", "aliases": ["CVE-2026-9703"], "summary": "ransomware", "affected": [], "references": []}])
        database.import_kev_json({"vulnerabilities": [{"cveID": "CVE-2026-9703", "vendorProject": "Vendor", "product": "Product", "vulnerabilityName": "Example", "dateAdded": "2026-01-03", "shortDescription": "Description", "requiredAction": "Patch", "dueDate": "2026-01-24", "knownRansomwareCampaignUse": "Yes"}]})
        report = database.exploitation_intelligence_report("CVE-2026-9703")
        self.assertEqual(report["ransomware_signal"]["status"], "known")
        self.assertEqual(report["ransomware_signal"]["source"], "cisa-kev")

    def test_rejects_unsupported_values(self):
        for value in (1, "Maybe", [], {}):
            with self.assertRaises(ValueError):
                normalize_ransomware_signal(value)


if __name__ == "__main__":
    unittest.main()
