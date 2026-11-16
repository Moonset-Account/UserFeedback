import unittest

from src.patterns import DEFAULT_CONTEXT_RULES, DEFAULT_DETECTION_RULES, match_context_rules, match_rules, validate_context_rule_registry, validate_rule_registry


class Stage8CICDBatch1Tests(unittest.TestCase):
    workflow = ".github/workflows/ci.yml"

    def test_new_ci_registry_ids_are_present_and_valid(self):
        validate_rule_registry(DEFAULT_DETECTION_RULES)
        validate_context_rule_registry(DEFAULT_CONTEXT_RULES)
        ids = {rule.rule_id for rule in DEFAULT_DETECTION_RULES}
        ids.update(rule.rule_id for rule in DEFAULT_CONTEXT_RULES)
        self.assertTrue({"CRT-CI-010", "CRT-CI-011", "CRT-CI-012", "CRT-CI-013", "CRT-CI-014"}.issubset(ids))

    def test_script_injection_fixtures(self):
        fixtures = [
            ("CRT-CI-010", "run: echo ${{ github.event.pull_request.title }}"),
            ("CRT-CI-011", "run: echo ${{ github.head_ref }}"),
            ("CRT-CI-012", "run: echo ${{ github.event.issue.body }}"),
            ("CRT-CI-013", "clean: false"),
        ]
        for rule_id, line in fixtures:
            with self.subTest(rule_id=rule_id):
                self.assertIn(rule_id, {rule.rule_id for rule, _ in match_rules(line, self.workflow)})

    def test_line_rules_do_not_match_safe_controls_or_unrelated_files(self):
        negatives = [
            ("CRT-CI-010", "run: echo ${{ steps.build.outputs.title }}"),
            ("CRT-CI-011", "run: echo ${{ github.sha }}"),
            ("CRT-CI-012", "run: echo fixed workflow text"),
            ("CRT-CI-013", "clean: true"),
        ]
        for rule_id, line in negatives:
            with self.subTest(rule_id=rule_id):
                self.assertNotIn(rule_id, {rule.rule_id for rule, _ in match_rules(line, self.workflow)})
                self.assertNotIn(rule_id, {rule.rule_id for rule, _ in match_rules(line, "docs/workflow.md")})

    def test_self_hosted_pull_request_context(self):
        positive = [(1, "on:"), (2, "  pull_request:"), (8, "runs-on: self-hosted")]
        ids = {match.rule.rule_id for match in match_context_rules(positive, self.workflow)}
        self.assertIn("CRT-CI-014", ids)
        negative = [(1, "on:"), (2, "  push:"), (8, "runs-on: self-hosted")]
        ids = {match.rule.rule_id for match in match_context_rules(negative, self.workflow)}
        self.assertNotIn("CRT-CI-014", ids)

    def test_batch_metadata_is_ci_policy(self):
        rules = [rule for rule in DEFAULT_DETECTION_RULES + DEFAULT_CONTEXT_RULES if rule.rule_id in {"CRT-CI-010", "CRT-CI-011", "CRT-CI-012", "CRT-CI-013", "CRT-CI-014"}]
        self.assertEqual(len(rules), 5)
        self.assertTrue(all(rule.category == "ci" and rule.kind == "policy" and rule.confidence == "high" for rule in rules))
        self.assertTrue(all(rule.file_globs for rule in rules))


if __name__ == "__main__":
    unittest.main()
