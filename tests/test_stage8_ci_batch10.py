import unittest

from src.patterns import DEFAULT_CONTEXT_RULES, DEFAULT_DETECTION_RULES, match_rules, validate_context_rule_registry, validate_rule_registry


class Stage8CICDBatch10Tests(unittest.TestCase):
    workflow = ".github/workflows/ci.yml"

    def test_batch_id_is_present_and_valid(self):
        validate_rule_registry(DEFAULT_DETECTION_RULES)
        validate_context_rule_registry(DEFAULT_CONTEXT_RULES)
        ids = {rule.rule_id for rule in DEFAULT_DETECTION_RULES}
        ids.update(rule.rule_id for rule in DEFAULT_CONTEXT_RULES)
        self.assertIn("CRT-CI-048", ids)

    def test_github_token_run_fixture(self):
        found = {rule.rule_id for rule, _ in match_rules("run: curl -H 'Authorization: Bearer ${{ github.token }}' https://example.test", self.workflow)}
        self.assertIn("CRT-CI-048", found)

    def test_safe_token_handling_and_unrelated_files_do_not_match(self):
        safe = (
            "    env: GITHUB_TOKEN: ${{ github.token }}",
            "      token: ${{ github.token }}",
            "run: echo ${{ secrets.GITHUB_TOKEN }}",
        )
        for line in safe:
            self.assertNotIn("CRT-CI-048", {rule.rule_id for rule, _ in match_rules(line, self.workflow)})
            self.assertNotIn("CRT-CI-048", {rule.rule_id for rule, _ in match_rules(line, "docs/workflow.md")})

    def test_batch_metadata_is_ci_policy(self):
        rule = next(rule for rule in DEFAULT_DETECTION_RULES + DEFAULT_CONTEXT_RULES if rule.rule_id == "CRT-CI-048")
        self.assertEqual((rule.category, rule.kind, rule.severity, rule.confidence), ("ci", "policy", "high", "high"))
        self.assertTrue(rule.file_globs)


if __name__ == "__main__":
    unittest.main()
