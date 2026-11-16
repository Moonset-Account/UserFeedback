import unittest

from src.patterns import DEFAULT_CONTEXT_RULES, DEFAULT_DETECTION_RULES, match_rules, validate_context_rule_registry, validate_rule_registry


class Stage8CICDBatch6Tests(unittest.TestCase):
    workflow = ".github/workflows/ci.yml"

    CASES = (
        ("CRT-CI-035", "run: echo ${{ github.event.client_payload.command }}"),
        ("CRT-CI-036", "run: echo ${{ github.event.inputs.target }}"),
        ("CRT-CI-037", "run: echo ${{ inputs.environment }}"),
    )

    def test_batch_ids_are_present_and_valid(self):
        validate_rule_registry(DEFAULT_DETECTION_RULES)
        validate_context_rule_registry(DEFAULT_CONTEXT_RULES)
        ids = {rule.rule_id for rule in DEFAULT_DETECTION_RULES}
        ids.update(rule.rule_id for rule in DEFAULT_CONTEXT_RULES)
        self.assertTrue({case[0] for case in self.CASES}.issubset(ids))

    def test_caller_controlled_context_fixtures(self):
        for rule_id, line in self.CASES:
            with self.subTest(rule_id=rule_id):
                self.assertIn(rule_id, {rule.rule_id for rule, _ in match_rules(line, self.workflow)})

    def test_safe_controls_and_unrelated_files_do_not_match(self):
        safe = (
            "run: echo ${{ github.sha }}",
            "run: echo ${{ vars.DEPLOY_TARGET }}",
            "run: echo fixed workflow text",
        )
        expected = {case[0] for case in self.CASES}
        for line in safe:
            found = {rule.rule_id for rule, _ in match_rules(line, self.workflow)}
            self.assertTrue(expected.isdisjoint(found))
            self.assertTrue(expected.isdisjoint({rule.rule_id for rule, _ in match_rules(line, "docs/workflow.md")}))

    def test_batch_metadata_is_ci_policy(self):
        wanted = {case[0] for case in self.CASES}
        rules = [rule for rule in DEFAULT_DETECTION_RULES + DEFAULT_CONTEXT_RULES if rule.rule_id in wanted]
        self.assertEqual(len(rules), 3)
        self.assertTrue(all(rule.category == "ci" and rule.kind == "policy" and rule.confidence == "high" for rule in rules))
        self.assertTrue(all(rule.file_globs for rule in rules))


if __name__ == "__main__":
    unittest.main()
