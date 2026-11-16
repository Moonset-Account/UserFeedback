import unittest

from src.patterns import DEFAULT_DETECTION_RULES, match_secret


class ShopifyV2DetectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rules = [
            next(r for r in DEFAULT_DETECTION_RULES if r.rule_id == rule_id)
            for rule_id in ("CRT-SEC-185", "CRT-SEC-186", "CRT-SEC-187", "CRT-SEC-188")
        ]

    def test_official_38_character_payload_variants(self):
        values = (
            "shpat_" + "a" * 38,
            "shpca_" + "b" * 38,
            "shppa_" + "c" * 38,
            "shpss_" + "d" * 38,
        )
        for value, rule in zip(values, self.rules):
            matches = match_secret(value, [rule])
            self.assertEqual([matched.rule_id for matched, _ in matches], [rule.rule_id])

    def test_assignment_positive(self):
        value = "shpat_" + "a" * 38
        matches = match_secret("SHOPIFY_ACCESS_TOKEN=" + value, [self.rules[0]])
        self.assertEqual([rule.rule_id for rule, _ in matches], ["CRT-SEC-185"])

    def test_legacy_32_character_payload_is_not_v2(self):
        value = "shpat_" + "a" * 32
        self.assertFalse(match_secret(value, self.rules))

    def test_short_and_long_payloads_are_rejected(self):
        for length in (37, 39):
            value = "shpca_" + ("b" * length)
            self.assertFalse(match_secret(value, [self.rules[1]]))

    def test_non_hex_payload_is_rejected(self):
        value = "shppa_" + ("g" * 38)
        self.assertFalse(match_secret(value, [self.rules[2]]))


if __name__ == "__main__":
    unittest.main()
