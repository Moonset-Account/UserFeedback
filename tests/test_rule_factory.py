import unittest

from src.rule_factory import RuleFactory, qualify_rule
from src.patterns import DetectionRule


BASE = {
    "name": "SYNTHETIC_RULE", "regex": "SYNTHETIC_TOKEN",
    "severity": "high", "description": "Synthetic detector",
    "rule_id": "CRT-SEC-901", "category": "secret", "confidence": "high",
    "remediation": "Rotate the affected value.", "kind": "secret", "file_globs": [],
    "provenance": {"source": "vendor docs", "url": "https://example.invalid/docs", "license": "vendor-documentation"},
}


class RuleFactoryTests(unittest.TestCase):
    def test_factory_returns_qualified_rule(self):
        rule, qualification = RuleFactory.from_mapping(dict(BASE))
        self.assertEqual(rule.rule_id, "CRT-SEC-901")
        self.assertTrue(qualification.qualified)

    def test_factory_rejects_missing_provenance(self):
        candidate = dict(BASE)
        candidate["provenance"] = {"source": "docs"}
        with self.assertRaisesRegex(ValueError, "qualification"):
            RuleFactory.from_mapping(candidate)

    def test_qualifier_reports_missing_metadata(self):
        rule = DetectionRule("X", "TOKEN", "high", "description")
        result = qualify_rule(rule)
        self.assertFalse(result.qualified)
        self.assertIn("missing stable rule_id", result.reasons)
        self.assertIn("missing provenance", result.reasons)

    def test_factory_rejects_unsafe_regex(self):
        candidate = dict(BASE)
        candidate["regex"] = "(a|b)+"
        with self.assertRaises(ValueError):
            RuleFactory.from_mapping(candidate)


if __name__ == "__main__":
    unittest.main()
