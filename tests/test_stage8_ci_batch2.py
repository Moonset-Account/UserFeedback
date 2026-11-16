import unittest

from src.patterns import DEFAULT_CONTEXT_RULES, DEFAULT_DETECTION_RULES, match_context_rules, match_rules, validate_context_rule_registry, validate_rule_registry


class Stage8CICDBatch2Tests(unittest.TestCase):
    workflow = ".github/workflows/ci.yml"

    def test_batch_ids_are_present_and_valid(self):
        validate_rule_registry(DEFAULT_DETECTION_RULES)
        validate_context_rule_registry(DEFAULT_CONTEXT_RULES)
        ids = {rule.rule_id for rule in DEFAULT_DETECTION_RULES}
        ids.update(rule.rule_id for rule in DEFAULT_CONTEXT_RULES)
        self.assertTrue({"CRT-CI-015", "CRT-CI-016", "CRT-CI-017", "CRT-CI-018", "CRT-CI-019"}.issubset(ids))

    def test_line_policy_fixtures(self):
        fixtures = [
            ("CRT-CI-015", "run: curl https://example.invalid/tool | bash"),
            ("CRT-CI-016", "- /var/run/docker.sock:/var/run/docker.sock"),
            ("CRT-CI-017", "container: ubuntu:latest"),
        ]
        for rule_id, line in fixtures:
            with self.subTest(rule_id=rule_id):
                self.assertIn(rule_id, {rule.rule_id for rule, _ in match_rules(line, self.workflow)})

    def test_line_policy_negative_controls(self):
        negatives = [
            ("CRT-CI-015", "run: curl https://example.invalid/tool -o tool"),
            ("CRT-CI-016", "- /var/run/docker.sock:/tmp/docker.sock"),
            ("CRT-CI-017", "container: ubuntu@sha256:fixture_digest"),
        ]
        for rule_id, line in negatives:
            with self.subTest(rule_id=rule_id):
                self.assertNotIn(rule_id, {rule.rule_id for rule, _ in match_rules(line, self.workflow)})
                self.assertNotIn(rule_id, {rule.rule_id for rule, _ in match_rules(line, "docs/workflow.md")})

    def test_workflow_run_untrusted_checkout_context(self):
        lines = [
            (1, "on:"),
            (2, "  workflow_run:"),
            (8, "uses: actions/checkout@v4"),
            (10, "ref: ${{ github.event.workflow_run.head_sha }}"),
        ]
        ids = {match.rule.rule_id for match in match_context_rules(lines, self.workflow)}
        self.assertIn("CRT-CI-018", ids)
        safe = [(1, "on:"), (2, "  push:"), (8, "uses: actions/checkout@v4"), (10, "ref: ${{ github.sha }}")]
        ids = {match.rule.rule_id for match in match_context_rules(safe, self.workflow)}
        self.assertNotIn("CRT-CI-018", ids)

    def test_workflow_run_self_hosted_context(self):
        lines = [(1, "on:"), (2, "  workflow_run:"), (9, "runs-on: self-hosted")]
        ids = {match.rule.rule_id for match in match_context_rules(lines, self.workflow)}
        self.assertIn("CRT-CI-019", ids)
        safe = [(1, "on:"), (2, "  push:"), (9, "runs-on: self-hosted")]
        ids = {match.rule.rule_id for match in match_context_rules(safe, self.workflow)}
        self.assertNotIn("CRT-CI-019", ids)

    def test_batch_metadata_is_ci_policy(self):
        wanted = {"CRT-CI-015", "CRT-CI-016", "CRT-CI-017", "CRT-CI-018", "CRT-CI-019"}
        rules = [rule for rule in DEFAULT_DETECTION_RULES + DEFAULT_CONTEXT_RULES if rule.rule_id in wanted]
        self.assertEqual(len(rules), 5)
        self.assertTrue(all(rule.category == "ci" and rule.kind == "policy" and rule.confidence == "high" for rule in rules))
        self.assertTrue(all(rule.file_globs for rule in rules))


if __name__ == "__main__":
    unittest.main()
