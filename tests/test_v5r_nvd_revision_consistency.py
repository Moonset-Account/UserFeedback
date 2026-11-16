"""Regression tests: NVD re-import must not mix revisions in one report."""

import copy
import json
import unittest

from src.vulnerability.database import VulnerabilityDatabase


CVE_ID = "CVE-2026-81001"
OTHER_CVE_ID = "CVE-2026-81002"

NVD_REV1 = {
    "cve": {
        "id": CVE_ID,
        "published": "2026-01-01T00:00:00.000",
        "lastModified": "2026-01-02T00:00:00.000",
        "vulnStatus": "Analyzed",
        "descriptions": [{"lang": "en", "value": "fictional revision one"}],
        "metrics": {"cvssMetricV31": [{"source": "nvd@nist.gov", "type": "Primary", "cvssData": {"version": "3.1", "vectorString": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H", "baseScore": 9.8}}]},
        "weaknesses": [{"description": [{"lang": "en", "value": "CWE-89"}]}],
        "configurations": [{"nodes": [{"cpeMatch": [{"vulnerable": True, "criteria": "cpe:2.3:a:fictional:widget:*:*:*:*:*:*:*:*", "versionEndExcluding": "2.0"}]}]}],
    }
}

NVD_REV2 = {
    "cve": {
        "id": CVE_ID,
        "published": "2026-01-01T00:00:00.000",
        "lastModified": "2026-02-01T00:00:00.000",
        "vulnStatus": "Modified",
        "descriptions": [{"lang": "en", "value": "fictional revision two"}],
        "metrics": {"cvssMetricV31": [{"source": "nvd@nist.gov", "type": "Primary", "cvssData": {"version": "3.1", "vectorString": "CVSS:3.1/AV:L/AC:H/PR:H/UI:R/S:U/C:L/I:L/A:L", "baseScore": 4.2}}]},
        "weaknesses": [{"description": [{"lang": "en", "value": "CWE-787"}]}],
        "configurations": [{"nodes": [{"cpeMatch": [{"vulnerable": True, "criteria": "cpe:2.3:a:fictional:widget:*:*:*:*:*:*:*:*", "versionEndExcluding": "2.1"}]}]}],
    }
}

NVD_OTHER = {
    "cve": {
        "id": OTHER_CVE_ID,
        "published": "2026-01-03T00:00:00.000",
        "lastModified": "2026-01-04T00:00:00.000",
        "vulnStatus": "Analyzed",
        "descriptions": [{"lang": "en", "value": "fictional other advisory"}],
        "metrics": {"cvssMetricV31": [{"source": "nvd@nist.gov", "type": "Primary", "cvssData": {"version": "3.1", "vectorString": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:H", "baseScore": 7.5}}]},
        "weaknesses": [{"description": [{"lang": "en", "value": "CWE-400"}]}],
        "configurations": [{"nodes": [{"cpeMatch": [{"vulnerable": True, "criteria": "cpe:2.3:a:fictional:gadget:*:*:*:*:*:*:*:*", "versionEndExcluding": "1.0"}]}]}],
    }
}


def _database_with_advisories() -> VulnerabilityDatabase:
    database = VulnerabilityDatabase()
    database.import_osv_records([
        {"id": "OSV-FICT-1", "aliases": [CVE_ID], "summary": "fictional one", "affected": [], "references": []},
        {"id": "OSV-FICT-2", "aliases": [OTHER_CVE_ID], "summary": "fictional two", "affected": [], "references": []},
    ])
    return database


class TestV5rNvdRevisionConsistency(unittest.TestCase):
    def test_first_import_report_matches_first_revision(self):
        database = _database_with_advisories()
        self.assertEqual(database.import_nvd_json(copy.deepcopy(NVD_REV1)).advisories_imported, 1)
        report = database.nvd_normalized_report(CVE_ID)
        self.assertEqual([item["data"]["baseScore"] for item in report["cvss"]], [9.8])
        self.assertEqual([item["data"]["value"] for item in report["weaknesses"]], ["CWE-89"])
        self.assertEqual([item["data"]["versionEndExcluding"] for item in report["cpe_matches"]], ["2.0"])
        self.assertEqual(database.nvd_enrichment(CVE_ID)["descriptions"][0]["value"], "fictional revision one")

    def test_second_import_report_contains_only_new_revision(self):
        database = _database_with_advisories()
        database.import_nvd_json(copy.deepcopy(NVD_REV1))
        old_report = database.nvd_normalized_report(CVE_ID)
        self.assertEqual([item["data"]["baseScore"] for item in old_report["cvss"]], [9.8])
        self.assertEqual(database.import_nvd_json(copy.deepcopy(NVD_REV2)).advisories_imported, 1)
        report = database.nvd_normalized_report(CVE_ID)
        self.assertEqual([item["data"]["baseScore"] for item in report["cvss"]], [4.2])
        self.assertEqual([item["data"]["value"] for item in report["weaknesses"]], ["CWE-787"])
        self.assertEqual([item["data"]["versionEndExcluding"] for item in report["cpe_matches"]], ["2.1"])
        for item in report["cvss"] + report["weaknesses"] + report["cpe_matches"]:
            self.assertEqual(item["content_digest"], report["revision_digest"])
        self.assertEqual(database.nvd_enrichment(CVE_ID)["content_digest"], report["revision_digest"])
        self.assertEqual(database.nvd_enrichment(CVE_ID)["descriptions"][0]["value"], "fictional revision two")
        for table in ("nvd_cvss", "nvd_weaknesses", "nvd_cpe_matches"):
            count = database.connection.execute(f"SELECT COUNT(*) FROM {table} WHERE cve_id = ?", (CVE_ID,)).fetchone()[0]
            self.assertEqual(count, 1, table)

    def test_reimport_same_content_keeps_report_identical(self):
        database = _database_with_advisories()
        database.import_nvd_json(copy.deepcopy(NVD_REV1))
        first = database.nvd_normalized_report(CVE_ID)
        stats = database.import_nvd_json(copy.deepcopy(NVD_REV1))
        self.assertEqual(stats.advisories_imported, 0)
        second = database.nvd_normalized_report(CVE_ID)
        self.assertEqual(first, second)

    def test_other_advisory_report_is_unaffected_by_reimport(self):
        database = _database_with_advisories()
        database.import_nvd_json(copy.deepcopy(NVD_REV1))
        database.import_nvd_json(copy.deepcopy(NVD_OTHER))
        other_before = database.nvd_normalized_report(OTHER_CVE_ID)
        database.import_nvd_json(copy.deepcopy(NVD_REV2))
        self.assertEqual(database.nvd_normalized_report(OTHER_CVE_ID), other_before)
        self.assertEqual([item["data"]["baseScore"] for item in other_before["cvss"]], [7.5])

    def test_legacy_untagged_rows_remain_readable_and_are_replaced_on_reimport(self):
        database = _database_with_advisories()
        database.import_nvd_json(copy.deepcopy(NVD_REV1))
        # Simulate pre-fix存量数据: detail rows without revision tagging.
        for table in ("nvd_cvss", "nvd_weaknesses", "nvd_cpe_matches"):
            database.connection.execute(f"UPDATE {table} SET content_digest = NULL WHERE cve_id = ?", (CVE_ID,))
        database.connection.commit()
        legacy_report = database.nvd_normalized_report(CVE_ID)
        self.assertEqual([item["data"]["baseScore"] for item in legacy_report["cvss"]], [9.8])
        self.assertEqual([item["data"]["value"] for item in legacy_report["weaknesses"]], ["CWE-89"])
        database.import_nvd_json(copy.deepcopy(NVD_REV2))
        report = database.nvd_normalized_report(CVE_ID)
        self.assertEqual([item["data"]["baseScore"] for item in report["cvss"]], [4.2])
        self.assertEqual([item["data"]["value"] for item in report["weaknesses"]], ["CWE-787"])
        self.assertEqual([item["data"]["versionEndExcluding"] for item in report["cpe_matches"]], ["2.1"])
        null_rows = database.connection.execute(
            "SELECT COUNT(*) FROM nvd_cvss WHERE cve_id = ? AND content_digest IS NULL", (CVE_ID,)
        ).fetchone()[0]
        self.assertEqual(null_rows, 0)

    def test_report_serializes_to_json(self):
        database = _database_with_advisories()
        database.import_nvd_json(copy.deepcopy(NVD_REV1))
        database.import_nvd_json(copy.deepcopy(NVD_REV2))
        json.dumps(database.nvd_normalized_report(CVE_ID))


if __name__ == "__main__":
    unittest.main()
