import unittest

from src.patterns import DEFAULT_CONTEXT_RULES, DEFAULT_DETECTION_RULES, match_context_rules, validate_context_rule_registry, validate_rule_registry


class Stage8CICDBatch18Tests(unittest.TestCase):
    workflow = ".github/workflows/artifacts.yml"

    def test_batch_id_is_present_and_valid(self):
        validate_rule_registry(DEFAULT_DETECTION_RULES)
        validate_context_rule_registry(DEFAULT_CONTEXT_RULES)
        ids = {rule.rule_id for rule in DEFAULT_DETECTION_RULES}
        ids.update(rule.rule_id for rule in DEFAULT_CONTEXT_RULES)
        self.assertIn("CRT-CI-056", ids)

    def test_upload_artifact_hidden_files_context_is_detected(self):
        content = "\n".join(
            [
                "      - uses: actions/upload-artifact@v4",
                "        with:",
                "          include-hidden-files: true",
            ]
        )
        found = {match.rule.rule_id for match in match_context_rules(list(enumerate(content.splitlines(), start=1)), self.workflow)}
        self.assertIn("CRT-CI-056", found)

    def test_false_controls_do_not_match(self):
        controls = (
            "      - uses: actions/upload-artifact@v4\n        with:\n          include-hidden-files: false",
            "      - uses: acme/other-action@v4\n        with:\n          include-hidden-files: true",
        )
        for content in controls:
            found = {match.rule.rule_id for match in match_context_rules(list(enumerate(content.splitlines(), start=1)), self.workflow)}
            self.assertNotIn("CRT-CI-056", found)

    def test_batch_metadata_is_ci_policy(self):
        rule = next(rule for rule in DEFAULT_CONTEXT_RULES if rule.rule_id == "CRT-CI-056")
        self.assertEqual((rule.category, rule.kind, rule.severity, rule.confidence), ("ci", "policy", "high", "high"))
        self.assertTrue(rule.file_globs)


if __name__ == "__main__":
    unittest.main()
