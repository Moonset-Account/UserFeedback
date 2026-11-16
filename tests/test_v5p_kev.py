"""RED tests for strict offline CISA KEV import."""

import unittest

from src.vulnerability.database import VulnerabilityDatabase
from src.vulnerability.sources.kev import parse_kev_record

KEV = {"cveID": "CVE-2025-9601", "vendorProject": "Vendor", "product": "Product", "vulnerabilityName": "Example", "dateAdded": "2025-01-03", "shortDescription": "KEV description", "requiredAction": "Apply mitigations", "dueDate": "2025-01-24", "knownRansomwareCampaignUse": "Unknown", "notes": "fixture"}


class TestV5pKev(unittest.TestCase):
    def test_parser_preserves_required_action_and_ransomware_flag(self):
        parsed = parse_kev_record(KEV)
        self.assertEqual(parsed["cve_id"], "CVE-2025-9601")
        self.assertEqual(parsed["required_action"], "Apply mitigations")
        self.assertEqual(parsed["known_ransomware_campaign_use"], "Unknown")

    def test_import_attaches_only_to_exact_existing_cve_alias(self):
        database = VulnerabilityDatabase()
        database.import_osv_records([{"id": "OSV-KEV-1", "aliases": ["CVE-2025-9601"], "summary": "OSV", "affected": [], "references": []}])
        stats = database.import_kev_json({"catalogVersion": "2025.01", "vulnerabilities": [KEV]})
        self.assertEqual(stats.advisories_imported, 1)
        record = database.kev_record("CVE-2025-9601")
        self.assertEqual(record["advisory_id"], "OSV-KEV-1")
        self.assertEqual(record["source"], "cisa-kev")

    def test_parser_rejects_invalid_kev_dates(self):
        for field, value in (("dateAdded", "2025-02-30"), ("dueDate", "2025/01/24")):
            record = dict(KEV)
            record[field] = value
            with self.assertRaises(ValueError):
                parse_kev_record(record)

    def test_parser_rejects_invalid_ransomware_flag_type(self):
        record: dict[str, object] = dict(KEV)
        record["knownRansomwareCampaignUse"] = 1
        with self.assertRaises(ValueError):
            parse_kev_record(record)

    def test_unknown_cve_is_rejected_without_record(self):
        database = VulnerabilityDatabase()
        stats = database.import_kev_json({"vulnerabilities": [KEV]})
        self.assertEqual(stats.advisories_imported, 0)
        with self.assertRaises(KeyError):
            database.kev_record("CVE-2025-9601")
    def test_import_reports_malformed_json(self):
        stats = VulnerabilityDatabase().import_kev_json("{not-json")
        self.assertEqual(stats.records_seen, 0)
        self.assertTrue(stats.errors)



if __name__ == "__main__":
    unittest.main()
