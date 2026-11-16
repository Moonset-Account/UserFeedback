import unittest

from src.patterns import DEFAULT_CONTEXT_RULES, DEFAULT_DETECTION_RULES, match_context_rules, match_rules, validate_context_rule_registry, validate_rule_registry


class Stage8CICDBatch3Tests(unittest.TestCase):
    workflow = ".github/workflows/ci.yml"

    def test_batch_ids_are_present_and_valid(self):
        validate_rule_registry(DEFAULT_DETECTION_RULES)
        validate_context_rule_registry(DEFAULT_CONTEXT_RULES)
        ids = {rule.rule_id for rule in DEFAULT_DETECTION_RULES}
        ids.update(rule.rule_id for rule in DEFAULT_CONTEXT_RULES)
        self.assertTrue({"CRT-CI-020", "CRT-CI-021", "CRT-CI-022", "CRT-CI-023", "CRT-CI-024"}.issubset(ids))

    def test_secret_interpolation_fixture(self):
        line = "run: deploy --token ${{ secrets.DEPLOY_TOKEN }}"
        self.assertIn("CRT-CI-020", {rule.rule_id for rule, _ in match_rules(line, self.workflow)})
        safe = "run: deploy --token $TOKEN"
        self.assertNotIn("CRT-CI-020", {rule.rule_id for rule, _ in match_rules(safe, self.workflow)})
        self.assertNotIn("CRT-CI-020", {rule.rule_id for rule, _ in match_rules(line, "docs/workflow.md")})

    def test_pull_request_target_head_repository_context(self):
        lines = [(1, "on:"), (2, "  pull_request_target:"), (8, "uses: actions/checkout@v4"), (9, "repository: ${{ github.event.pull_request.head.repo.full_name }}")]
        ids = {match.rule.rule_id for match in match_context_rules(lines, self.workflow)}
        self.assertIn("CRT-CI-021", ids)
        safe = [(1, "on:"), (2, "  pull_request_target:"), (8, "repository: ${{ github.repository }}")]
        ids = {match.rule.rule_id for match in match_context_rules(safe, self.workflow)}
        self.assertNotIn("CRT-CI-021", ids)

    def test_pull_request_target_head_ref_checkout_context(self):
        lines = [(1, "on:"), (2, "  pull_request_target:"), (8, "uses: actions/checkout@v4"), (10, "ref: ${{ github.event.pull_request.head.ref }}")]
        ids = {match.rule.rule_id for match in match_context_rules(lines, self.workflow)}
        self.assertIn("CRT-CI-022", ids)

    def test_workflow_run_artifact_execution_context(self):
        lines = [(1, "on:"), (2, "  workflow_run:"), (8, "uses: actions/download-artifact@v4"), (12, "run: ./artifact/run.sh")]
        ids = {match.rule.rule_id for match in match_context_rules(lines, self.workflow)}
        self.assertIn("CRT-CI-023", ids)
        safe = [(1, "on:"), (2, "  workflow_run:"), (8, "uses: actions/download-artifact@v4"), (12, "run: sha256sum artifact.bin")]
        ids = {match.rule.rule_id for match in match_context_rules(safe, self.workflow)}
        self.assertNotIn("CRT-CI-023", ids)

    def test_pull_request_write_permissions_context(self):
        lines = [(1, "on:"), (2, "  pull_request:"), (8, "permissions:"), (9, "  contents: write")]
        ids = {match.rule.rule_id for match in match_context_rules(lines, self.workflow)}
        self.assertIn("CRT-CI-024", ids)
        safe = [(1, "on:"), (2, "  pull_request:"), (8, "permissions:"), (9, "  contents: read")]
        ids = {match.rule.rule_id for match in match_context_rules(safe, self.workflow)}
        self.assertNotIn("CRT-CI-024", ids)

    def test_batch_metadata_is_ci_policy(self):
        wanted = {"CRT-CI-020", "CRT-CI-021", "CRT-CI-022", "CRT-CI-023", "CRT-CI-024"}
        rules = [rule for rule in DEFAULT_DETECTION_RULES + DEFAULT_CONTEXT_RULES if rule.rule_id in wanted]
        self.assertEqual(len(rules), 5)
        self.assertTrue(all(rule.category == "ci" and rule.kind == "policy" and rule.confidence == "high" for rule in rules))
        self.assertTrue(all(rule.file_globs for rule in rules))


if __name__ == "__main__":
    unittest.main()
