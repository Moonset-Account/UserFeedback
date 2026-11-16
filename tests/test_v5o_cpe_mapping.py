"""RED tests for strict CPE parsing and explicit mapping."""

import unittest

from src.vulnerability.database import VulnerabilityDatabase
from src.vulnerability.sources.cpe import parse_cpe23

CPE = "cpe:2.3:a:vendor:product:1.2:*:*:*:*:*:*:*"
ESCAPED_CPE = "cpe:2.3:a:vendor:my\\:product:1.2:*:*:*:*:*:*:*"


class TestV5oCpeMapping(unittest.TestCase):
    def test_parser_returns_components_and_wildcard_state(self):
        parsed = parse_cpe23(CPE)
        self.assertEqual(parsed["part"], "a")
        self.assertEqual(parsed["vendor"], "vendor")
        self.assertEqual(parsed["product"], "product")
        self.assertTrue(parsed["is_wildcard"])

    def test_parser_unescapes_quoted_punctuation_in_product(self):
        parsed = parse_cpe23(ESCAPED_CPE)
        self.assertEqual(parsed["part"], "a")
        self.assertEqual(parsed["vendor"], "vendor")
        self.assertEqual(parsed["product"], "my:product")
        self.assertEqual(parsed["version"], "1.2")
        self.assertEqual(parsed["other"], "*")
        self.assertTrue(parsed["is_wildcard"])

    def test_parser_keeps_fixed_field_count_and_order_with_escapes(self):
        parsed = parse_cpe23(ESCAPED_CPE)
        self.assertEqual(
            [key for key in parsed if key not in {"binding", "is_wildcard"}],
            ["part", "vendor", "product", "version", "update", "edition", "language", "sw_edition", "target_sw", "target_hw", "other"],
        )

    def test_parser_treats_escaped_wildcards_as_literals(self):
        parsed = parse_cpe23("cpe:2.3:a:vendor:\\*:1.0:gold:en:en:sw:tsw:thw:o")
        self.assertEqual(parsed["product"], "*")
        self.assertFalse(parsed["is_wildcard"])

    def test_parser_rejects_illegal_escapes(self):
        with self.assertRaises(ValueError):
            parse_cpe23("cpe:2.3:a:ven\\dor:product:1.2:*:*:*:*:*:*:*")
        with self.assertRaises(ValueError):
            parse_cpe23("cpe:2.3:a:vendor:product:1.2:*:*:*:*:*:*:\\")

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

    def test_mapping_accepts_escaped_cpe_binding(self):
        database = VulnerabilityDatabase()
        mapping = database.record_cpe_mapping(ESCAPED_CPE, "pkg:pypi/my-product@1.2", confidence="high", operator_approved=True, rationale="operator fixture")
        self.assertEqual(mapping["status"], "recorded")
        self.assertEqual(database.cpe_mapping_report()["mappings"][0]["cpe_binding"], ESCAPED_CPE)

    def test_mapping_rejects_prefix_only_purl(self):
        database = VulnerabilityDatabase()
        for bad_purl in ("pkg:", "pkg:pypi", "pkg:/", "not-a-purl"):
            with self.assertRaises(ValueError) as caught:
                database.record_cpe_mapping(CPE, bad_purl, confidence="high", operator_approved=True, rationale="operator fixture")
            self.assertIn("PURL", str(caught.exception))
        self.assertEqual(database.cpe_mapping_report()["mapping_count"], 0)

    def test_mapping_error_identifies_cpe_binding(self):
        database = VulnerabilityDatabase()
        with self.assertRaises(ValueError) as caught:
            database.record_cpe_mapping("cpe:2.3:a:vendor:product", "pkg:pypi/product@1.2", confidence="high", operator_approved=True, rationale="operator fixture")
        self.assertIn("CPE", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
