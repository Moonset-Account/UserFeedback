import unittest

from src.patterns import DEFAULT_CONTEXT_RULES, DEFAULT_DETECTION_RULES, match_context_rules, validate_context_rule_registry, validate_rule_registry


class Stage8CICDBatch17Tests(unittest.TestCase):
    workflow = ".github/workflows/ci.yml"

    def test_batch_id_is_present_and_valid(self):
        validate_rule_registry(DEFAULT_DETECTION_RULES)
        validate_context_rule_registry(DEFAULT_CONTEXT_RULES)
        ids = {rule.rule_id for rule in DEFAULT_DETECTION_RULES}
        ids.update(rule.rule_id for rule in DEFAULT_CONTEXT_RULES)
        self.assertIn("CRT-CI-055", ids)

    def test_checkout_persistent_credentials_context_is_detected(self):
        content = "\n".join(
            [
                "      - uses: actions/checkout@v4",
                "        with:",
                "          persist-credentials: true",
            ]
        )
        found = {match.rule.rule_id for match in match_context_rules(list(enumerate(content.splitlines(), start=1)), self.workflow)}
        self.assertIn("CRT-CI-055", found)

    def test_false_controls_do_not_match(self):
        controls = (
            "      - uses: actions/checkout@v4\n        with:\n          persist-credentials: false",
            "      - uses: acme/other-action@v4\n        with:\n          persist-credentials: true",
        )
        for content in controls:
            found = {match.rule.rule_id for match in match_context_rules(list(enumerate(content.splitlines(), start=1)), self.workflow)}
            self.assertNotIn("CRT-CI-055", found)

    def test_batch_metadata_is_ci_policy(self):
        rule = next(rule for rule in DEFAULT_CONTEXT_RULES if rule.rule_id == "CRT-CI-055")
        self.assertEqual((rule.category, rule.kind, rule.severity, rule.confidence), ("ci", "policy", "high", "high"))
        self.assertTrue(rule.file_globs)


if __name__ == "__main__":
    unittest.main()
