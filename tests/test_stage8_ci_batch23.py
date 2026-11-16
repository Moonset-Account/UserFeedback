import unittest

from src.patterns import DEFAULT_CONTEXT_RULES, DEFAULT_DETECTION_RULES, match_context_rules, validate_context_rule_registry, validate_rule_registry


class Stage8CICDBatch23Tests(unittest.TestCase):
    workflow = ".github/workflows/package.yml"

    def test_batch_id_is_present_and_valid(self):
        validate_rule_registry(DEFAULT_DETECTION_RULES)
        validate_context_rule_registry(DEFAULT_CONTEXT_RULES)
        ids = {rule.rule_id for rule in DEFAULT_DETECTION_RULES}
        ids.update(rule.rule_id for rule in DEFAULT_CONTEXT_RULES)
        self.assertIn("CRT-CI-060", ids)

    def test_top_level_package_write_is_detected(self):
        content = "\n".join(["permissions:", "  packages: write"])
        found = {match.rule.rule_id for match in match_context_rules(list(enumerate(content.splitlines(), start=1)), self.workflow)}
        self.assertIn("CRT-CI-060", found)

    def test_job_level_or_read_permission_does_not_match(self):
        controls = (
            "permissions:\n  packages: read",
            "jobs:\n  publish:\n    permissions:\n      packages: write",
            "permissions:\n  contents: write",
        )
        for content in controls:
            found = {match.rule.rule_id for match in match_context_rules(list(enumerate(content.splitlines(), start=1)), self.workflow)}
            self.assertNotIn("CRT-CI-060", found)

    def test_batch_metadata_is_ci_policy(self):
        rule = next(rule for rule in DEFAULT_CONTEXT_RULES if rule.rule_id == "CRT-CI-060")
        self.assertEqual((rule.category, rule.kind, rule.severity, rule.confidence), ("ci", "policy", "medium", "high"))
        self.assertTrue(rule.file_globs)


if __name__ == "__main__":
    unittest.main()
