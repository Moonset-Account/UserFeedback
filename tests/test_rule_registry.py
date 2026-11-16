import unittest

from src.engine import RuleRegistry
from src.patterns import DEFAULT_DETECTION_RULES, DetectionRule


class RuleRegistryTests(unittest.TestCase):
    def test_registry_precompiles_all_rules(self):
        registry = RuleRegistry(DEFAULT_DETECTION_RULES)
        self.assertEqual(len(registry.rules), len(DEFAULT_DETECTION_RULES))
        self.assertTrue(all(item.compiled.pattern for item in registry.rules))

    def test_literal_prefilter_keeps_matching_provider(self):
        registry = RuleRegistry(DEFAULT_DETECTION_RULES)
        plan = registry.plan("src/config.py", "AWS_ACCESS_KEY_ID=AKIAAAAAAAAAAAAAAAAA")
        ids = {item.rule.rule_id for item in plan.candidates}
        self.assertIn("CRT-SEC-001", ids)

    def test_file_glob_excludes_scoped_policy(self):
        rule = DetectionRule(
            name="SCOPED", regex=r"danger", severity="high", description="test",
            rule_id="CRT-TEST-001", file_globs=("*.yaml",),
        )
        registry = RuleRegistry([rule])
        self.assertEqual(registry.plan("src/app.py", "danger").candidates, ())
        self.assertEqual(len(registry.plan("deploy.yaml", "danger").candidates), 1)

    def test_plan_is_deterministic(self):
        registry = RuleRegistry(DEFAULT_DETECTION_RULES)
        first = [item.rule.rule_id for item in registry.plan("config.env", "password=synthetic-secret").candidates]
        second = [item.rule.rule_id for item in registry.plan("config.env", "password=synthetic-secret").candidates]
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
