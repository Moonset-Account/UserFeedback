import unittest

from src.patterns import DEFAULT_CONTEXT_RULES, DEFAULT_DETECTION_RULES, match_context_rules, validate_context_rule_registry, validate_rule_registry


class Stage8CICDBatch19Tests(unittest.TestCase):
    workflow = ".github/workflows/cache.yml"

    def test_batch_id_is_present_and_valid(self):
        validate_rule_registry(DEFAULT_DETECTION_RULES)
        validate_context_rule_registry(DEFAULT_CONTEXT_RULES)
        ids = {rule.rule_id for rule in DEFAULT_DETECTION_RULES}
        ids.update(rule.rule_id for rule in DEFAULT_CONTEXT_RULES)
        self.assertIn("CRT-CI-057", ids)

    def test_privileged_trigger_cache_context_is_detected(self):
        content = "\n".join(
            [
                "on:",
                "  pull_request_target:",
                "jobs:",
                "  build:",
                "    steps:",
                "      - uses: actions/cache@v4",
            ]
        )
        found = {match.rule.rule_id for match in match_context_rules(list(enumerate(content.splitlines(), start=1)), self.workflow)}
        self.assertIn("CRT-CI-057", found)

    def test_false_controls_do_not_match(self):
        controls = (
            "on:\n  pull_request:\njobs:\n  build:\n    steps:\n      - uses: actions/cache@v4",
            "on:\n  pull_request_target:\njobs:\n  build:\n    steps:\n      - uses: actions/checkout@v4",
        )
        for content in controls:
            found = {match.rule.rule_id for match in match_context_rules(list(enumerate(content.splitlines(), start=1)), self.workflow)}
            self.assertNotIn("CRT-CI-057", found)

    def test_batch_metadata_is_ci_policy(self):
        rule = next(rule for rule in DEFAULT_CONTEXT_RULES if rule.rule_id == "CRT-CI-057")
        self.assertEqual((rule.category, rule.kind, rule.severity, rule.confidence), ("ci", "policy", "high", "high"))
        self.assertTrue(rule.file_globs)


if __name__ == "__main__":
    unittest.main()
