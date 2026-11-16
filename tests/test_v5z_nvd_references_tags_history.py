"""RED tests for NVD references, tags, and change history preservation."""

import copy
import unittest
from typing import Any

from src.vulnerability.database import VulnerabilityDatabase
from src.vulnerability.sources.nvd import parse_nvd_cve
from tests.test_v5m_nvd_enrichment import NVD


class TestV5zNvdReferencesTagsHistory(unittest.TestCase):
    def record(self):
        record: dict[str, Any] = copy.deepcopy(NVD)
        record["cve"]["references"] = [{
            "url": "https://vendor.example/advisory/CVE-2025-9601",
            "source": "security@vendor.example",
            "tags": ["vendor-advisory", "patch"],
        }]
        record["cve"]["cveTags"] = [{
            "sourceIdentifier": "security@vendor.example",
            "tags": ["disputed"],
        }]
        record["cveChanges"] = [{
            "changeId": "change-1",
            "created": "2025-01-03T00:00:00.000",
            "sourceIdentifier": "security@vendor.example",
            "eventName": "CVE Modified",
            "cveChangeChanges": [{"action": "Added", "type": "references", "newValue": "patch"}],
        }]
        return record

    def test_parser_preserves_references_tags_and_history(self):
        parsed = parse_nvd_cve(self.record())
        self.assertEqual(parsed["references"][0]["url"], "https://vendor.example/advisory/CVE-2025-9601")
        self.assertEqual(parsed["references"][0]["tags"], ["vendor-advisory", "patch"])
        self.assertEqual(parsed["tags"][0]["source_identifier"], "security@vendor.example")
        self.assertEqual(parsed["tags"][0]["tags"], ["disputed"])
        self.assertEqual(parsed["history"][0]["change_id"], "change-1")
        self.assertEqual(parsed["history"][0]["changes"][0]["new_value"], "patch")

    def test_malformed_references_tags_and_history_fail_closed(self):
        cases = [
            ("references", [{"url": 1}]),
            ("references", [{"url": "https://example.test", "tags": "patch"}]),
            ("cveTags", [{"sourceIdentifier": "source", "tags": [1]}]),
            ("cveChanges", [{"changeId": "x", "cveChangeChanges": "bad"}]),
        ]
        for key, value in cases:
            record = self.record()
            record["cve"][key] = value if key != "cveChanges" else record["cve"].get(key, value)
            if key == "cveChanges":
                record["cveChanges"] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                parse_nvd_cve(record)

    def test_normalized_report_readback_preserves_nvd_metadata(self):
        record = self.record()
        database = VulnerabilityDatabase()
        database.import_osv_records([{"id": "OSV-V5Z-1", "aliases": ["CVE-2025-9601"], "summary": "x", "affected": [], "references": []}])
        self.assertEqual(database.import_nvd_json(record).advisories_imported, 1)
        report = database.nvd_normalized_report("CVE-2025-9601")
        self.assertEqual(report["references"][0]["url"], record["cve"]["references"][0]["url"])
        self.assertEqual(report["tags"][0]["tags"], ["disputed"])
        self.assertEqual(report["history"][0]["change_id"], "change-1")


if __name__ == "__main__":
    unittest.main()
