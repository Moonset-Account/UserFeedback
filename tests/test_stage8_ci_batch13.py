import unittest

from src.patterns import DEFAULT_CONTEXT_RULES, DEFAULT_DETECTION_RULES, match_rules, validate_context_rule_registry, validate_rule_registry


class Stage8CICDBatch13Tests(unittest.TestCase):
    workflow = ".github/workflows/ci.yml"

    def test_batch_id_is_present_and_valid(self):
        validate_rule_registry(DEFAULT_DETECTION_RULES)
        validate_context_rule_registry(DEFAULT_CONTEXT_RULES)
        ids = {rule.rule_id for rule in DEFAULT_DETECTION_RULES}
        ids.update(rule.rule_id for rule in DEFAULT_CONTEXT_RULES)
        self.assertIn("CRT-CI-051", ids)

    def test_prerelease_tag_fixtures(self):
        for line in (
            "    uses: acme/build-action@v4.0.0-beta",
            "    uses: acme/build-action@v2-rc1",
        ):
            found = {rule.rule_id for rule, _ in match_rules(line, self.workflow)}
            self.assertIn("CRT-CI-051", found)

    def test_safe_controls_do_not_match(self):
        for line in (
            "    uses: acme/build-action@v4",
            "    uses: acme/build-action@0123456789abcdef0123456789abcdef01234567",
            "    uses: ./ .github/actions/local",
        ):
            self.assertNotIn("CRT-CI-051", {rule.rule_id for rule, _ in match_rules(line, self.workflow)})

    def test_batch_metadata_is_ci_policy(self):
        rule = next(rule for rule in DEFAULT_DETECTION_RULES + DEFAULT_CONTEXT_RULES if rule.rule_id == "CRT-CI-051")
        self.assertEqual((rule.category, rule.kind, rule.severity, rule.confidence), ("ci", "policy", "medium", "high"))
        self.assertTrue(rule.file_globs)


if __name__ == "__main__":
    unittest.main()
