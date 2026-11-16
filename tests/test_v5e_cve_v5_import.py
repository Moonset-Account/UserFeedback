"""RED tests for V5e strict CVE JSON 5 parsing and offline import."""

import unittest

from src.vulnerability.database import VulnerabilityDatabase
from src.vulnerability.sources.cve_v5 import parse_cve_v5_record


CVE = {
    "dataType": "CVE_RECORD",
    "dataVersion": "5.1",
    "cveMetadata": {
        "cveId": "CVE-2025-7001",
        "assignerOrgId": "fixture-org",
        "state": "PUBLISHED",
        "datePublished": "2025-01-02T03:04:05Z",
        "dateUpdated": "2025-01-03T03:04:05Z",
    },
    "containers": {
        "cna": {
            "descriptions": [{"lang": "en", "value": "CVE fixture details"}],
            "references": [{"url": "https://example.invalid/advisory", "tags": ["vendor-advisory"]}],
        },
        "adp": [{"providerMetadata": {"orgId": "adp-org"}, "references": [{"url": "https://example.invalid/adp"}]}],
    },
}


class TestV5eCVEV5Import(unittest.TestCase):
    def test_parser_preserves_cve_metadata_descriptions_and_cna_adp_references(self):
        parsed = parse_cve_v5_record(CVE)
        self.assertEqual(parsed["id"], "CVE-2025-7001")
        self.assertEqual(parsed["status"], "PUBLISHED")
        self.assertEqual(parsed["published"], "2025-01-02T03:04:05Z")
        self.assertEqual(parsed["details"], "CVE fixture details")
        self.assertEqual({item["url"] for item in parsed["references"]}, {"https://example.invalid/advisory", "https://example.invalid/adp"})

    def test_database_import_keeps_cve_source_record_without_guessing_package_mapping(self):
        database = VulnerabilityDatabase()
        result = database.import_cve_v5_json(CVE)
        self.assertEqual(result.advisories_imported, 1)
        metadata = database.advisory_metadata("CVE-2025-7001")
        self.assertEqual(metadata["source"], "cve-v5")
        self.assertEqual(database.affected_package_count(), 0)
        self.assertEqual(database.source_record_revision_count("cve-v5", "CVE-2025-7001"), 1)

    def test_rejected_and_reserved_are_retained_as_non_active_metadata(self):
        database = VulnerabilityDatabase()
        rejected = dict(CVE, cveMetadata=dict(CVE["cveMetadata"], cveId="CVE-2025-7002", state="REJECTED"))
        reserved = dict(CVE, cveMetadata=dict(CVE["cveMetadata"], cveId="CVE-2025-7003", state="RESERVED"))
        database.import_cve_v5_json([rejected, reserved])
        self.assertEqual(database.advisory_metadata("CVE-2025-7002")["withdrawn"], "rejected")
        self.assertEqual(database.advisory_metadata("CVE-2025-7003")["database_specific"]["source_status"], "RESERVED")

    def test_parser_rejects_wrong_type_missing_id_and_unknown_state(self):
        with self.assertRaises(ValueError):
            parse_cve_v5_record({"dataType": "OTHER", "cveMetadata": {}})
        with self.assertRaises(ValueError):
            parse_cve_v5_record({"dataType": "CVE_RECORD", "cveMetadata": {"state": "PUBLISHED"}})
        bad = dict(CVE, cveMetadata=dict(CVE["cveMetadata"], state="UNKNOWN"))
        with self.assertRaises(ValueError):
            parse_cve_v5_record(bad)


if __name__ == "__main__":
    unittest.main()
