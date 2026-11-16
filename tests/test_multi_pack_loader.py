import unittest
from unittest.mock import patch

from src.patterns import DetectionRule
from src.rulepacks import load_rule_packs


def _rule(rule_id):
    return DetectionRule(
        name="SYNTHETIC_RULE", regex=r"SYNTH_[A-Z]+", severity="high",
        description="Synthetic test rule", rule_id=rule_id, category="secret",
    )


class MultiPackLoaderTests(unittest.TestCase):
    def test_combines_signed_pack_results_in_input_order(self):
        with patch("src.rulepacks._load_rule_pack", side_effect=[([_rule("CRT-SEC-901")], b"a"), ([_rule("CRT-SEC-902")], b"b")]):
            rules = load_rule_packs(["one.json", "two.json"], {"test-key": b"x" * 32})
        self.assertEqual([rule.rule_id for rule in rules], ["CRT-SEC-901", "CRT-SEC-902"])

    def test_rejects_duplicate_ids_across_packs(self):
        with patch("src.rulepacks._load_rule_pack", side_effect=[([_rule("CRT-SEC-901")], b"a"), ([_rule("CRT-SEC-901")], b"b")]):
            with self.assertRaisesRegex(ValueError, "duplicate rule ID"):
                load_rule_packs(["one.json", "two.json"], {})

    def test_rejects_empty_or_unbounded_path_list(self):
        with self.assertRaises(ValueError):
            load_rule_packs([], {})
        with self.assertRaises(ValueError):
            load_rule_packs(["x.json"] * 65, {})

    def test_enforces_aggregate_byte_bound(self):
        with patch("src.rulepacks._load_rule_pack", return_value=([_rule("CRT-SEC-901")], b"x" * (8 * 1024 * 1024))):
            with self.assertRaisesRegex(ValueError, "aggregate bounds"):
                load_rule_packs(["one.json", "two.json"], {})


if __name__ == "__main__":
    unittest.main()
