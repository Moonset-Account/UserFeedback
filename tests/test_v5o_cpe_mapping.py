"""RED tests for strict CPE parsing and explicit mapping."""

import unittest

from src.vulnerability.database import VulnerabilityDatabase
from src.vulnerability.sources.cpe import parse_cpe23

CPE = "cpe:2.3:a:vendor:product:1.2:*:*:*:*:*:*:*"


class TestV5oCpeMapping(unittest.TestCase):
    def test_parser_returns_components_and_wildcard_state(self):
        parsed = parse_cpe23(CPE)
        self.assertEqual(parsed["part"], "a")
        self.assertEqual(parsed["vendor"], "vendor")
        self.assertEqual(parsed["product"], "product")
        self.assertTrue(parsed["is_wildcard"])

    def test_mapping_requires_explicit_operator_approval(self):
        database = VulnerabilityDatabase()
        with self.assertRaises(ValueError):
            database.record_cpe_mapping(CPE, "pkg:pypi/product@1.2", confidence="high", operator_approved=False, rationale="")
        mapping = database.record_cpe_mapping(CPE, "pkg:pypi/product@1.2", confidence="high", operator_approved=True, rationale="operator fixture")
        self.assertEqual(mapping["status"], "recorded")
        self.assertEqual(database.cpe_mapping_report()["mapping_count"], 1)
        self.assertEqual(database.cpe_mapping_report()["mappings"][0]["source"], "operator")

    def test_parser_rejects_wrong_binding_or_component_count(self):
        with self.assertRaises(ValueError):
            parse_cpe23("cpe:/a:vendor:product")
        with self.assertRaises(ValueError):
            parse_cpe23("cpe:2.3:a:vendor:product")


if __name__ == "__main__":
    unittest.main()
