"""RED tests for preserving NVD configuration logic."""

import copy
import unittest

from src.vulnerability.sources.nvd import parse_nvd_cve
from src.vulnerability.database import VulnerabilityDatabase
from tests.test_v5m_nvd_enrichment import NVD


class TestV5yNvdConfigurationSemantics(unittest.TestCase):
    def test_normalized_report_preserves_configuration_logic_after_import(self):
        record = copy.deepcopy(NVD)
        record["cve"]["configurations"] = [{"nodes": [{"operator": "AND", "negate": True, "cpeMatch": [{"criteria": "cpe:2.3:a:v:p:*:*:*:*:*:*:*:*"}]}]}]
        database = VulnerabilityDatabase()
        database.import_osv_records([{"id": "OSV-V5Y-1", "aliases": ["CVE-2025-9601"], "summary": "x", "affected": [], "references": []}])
        self.assertEqual(database.import_nvd_json(record).advisories_imported, 1)
        node = database.nvd_normalized_report("CVE-2025-9601")["configurations"][0]["nodes"][0]
        self.assertEqual(node["operator"], "AND")
        self.assertTrue(node["negate"])

    def test_parser_preserves_node_operator_and_negate(self):
        record = copy.deepcopy(NVD)
        record["cve"]["configurations"] = [{
            "nodes": [
                {"operator": "AND", "negate": True, "cpeMatch": [
                    {"vulnerable": True, "criteria": "cpe:2.3:a:vendor:product:*:*:*:*:*:*:*:*"},
                    {"vulnerable": False, "criteria": "cpe:2.3:o:vendor:os:*:*:*:*:*:*:*:*"},
                ]},
                {"operator": "OR", "cpeMatch": [
                    {"vulnerable": True, "criteria": "cpe:2.3:a:vendor:other:*:*:*:*:*:*:*:*"},
                ]},
            ]
        }]
        parsed = parse_nvd_cve(record)
        self.assertEqual(parsed["configurations"][0]["nodes"][0]["operator"], "AND")
        self.assertTrue(parsed["configurations"][0]["nodes"][0]["negate"])
        self.assertEqual(len(parsed["configurations"][0]["nodes"][0]["cpe_matches"]), 2)
        self.assertEqual(parsed["configurations"][0]["nodes"][1]["operator"], "OR")
        self.assertFalse(parsed["configurations"][0]["nodes"][1]["negate"])

    def test_parser_defaults_nvd_node_logic_and_rejects_invalid_values(self):
        record = copy.deepcopy(NVD)
        record["cve"]["configurations"] = [{"nodes": [{"cpeMatch": [{"criteria": "cpe:2.3:a:v:p:*:*:*:*:*:*:*:*"}]}]}]
        parsed = parse_nvd_cve(record)
        node = parsed["configurations"][0]["nodes"][0]
        self.assertEqual(node["operator"], "OR")
        self.assertFalse(node["negate"])
        for invalid in ({"operator": "XOR"}, {"operator": 1}, {"negate": "false"}):
            bad = copy.deepcopy(NVD)
            bad_node = {"cpeMatch": [{"criteria": "cpe:2.3:a:v:p:*:*:*:*:*:*:*:*"}], **invalid}
            bad["cve"]["configurations"] = [{"nodes": [bad_node]}]
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                parse_nvd_cve(bad)


if __name__ == "__main__":
    unittest.main()
