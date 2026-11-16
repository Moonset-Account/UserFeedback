import unittest

from src.patterns import DEFAULT_CONTEXT_RULES, DEFAULT_DETECTION_RULES, match_rules, validate_context_rule_registry, validate_rule_registry


class Stage8CICDBatch11Tests(unittest.TestCase):
    workflow = ".github/workflows/ci.yml"

    def test_batch_id_is_present_and_valid(self):
        validate_rule_registry(DEFAULT_DETECTION_RULES)
        validate_context_rule_registry(DEFAULT_CONTEXT_RULES)
        ids = {rule.rule_id for rule in DEFAULT_DETECTION_RULES}
        ids.update(rule.rule_id for rule in DEFAULT_CONTEXT_RULES)
        self.assertIn("CRT-CI-049", ids)

    def test_docker_action_latest_fixture(self):
        line = "    uses: docker://ghcr.io/acme/build:latest"
        found = {rule.rule_id for rule, _ in match_rules(line, self.workflow)}
        self.assertIn("CRT-CI-049", found)

    def test_pinned_and_unrelated_controls_do_not_match(self):
        safe = (
            "    uses: docker://ghcr.io/acme/build@sha256:abc123",
            "    uses: docker://ghcr.io/acme/build:v1.2.3",
            "    container: ghcr.io/acme/build:latest",
        )
        for line in safe:
            self.assertNotIn("CRT-CI-049", {rule.rule_id for rule, _ in match_rules(line, self.workflow)})
            self.assertNotIn("CRT-CI-049", {rule.rule_id for rule, _ in match_rules(line, "docs/workflow.md")})

    def test_batch_metadata_is_ci_policy(self):
        rule = next(rule for rule in DEFAULT_DETECTION_RULES + DEFAULT_CONTEXT_RULES if rule.rule_id == "CRT-CI-049")
        self.assertEqual((rule.category, rule.kind, rule.severity, rule.confidence), ("ci", "policy", "medium", "high"))
        self.assertTrue(rule.file_globs)


if __name__ == "__main__":
    unittest.main()
