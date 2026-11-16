import unittest
from src.patterns import DEFAULT_CONTEXT_RULES, DEFAULT_DETECTION_RULES, match_context_rules, validate_context_rule_registry, validate_rule_registry
class Stage8CICDBatch29Tests(unittest.TestCase):
    workflow = ".github/workflows/status.yml"
    def test_registry(self):
        validate_rule_registry(DEFAULT_DETECTION_RULES); validate_context_rule_registry(DEFAULT_CONTEXT_RULES)
        self.assertIn("CRT-CI-066", {r.rule_id for r in DEFAULT_CONTEXT_RULES})
    def test_top_level_statuses_write(self):
        content="permissions:\n  statuses: write"
        found={m.rule.rule_id for m in match_context_rules(list(enumerate(content.splitlines(),1)),self.workflow)}
        self.assertIn("CRT-CI-066",found)
    def test_safe_scopes_do_not_match(self):
        for content in ("permissions:\n  statuses: read", "jobs:\n  status:\n    permissions:\n      statuses: write", "permissions:\n  contents: write"):
            found={m.rule.rule_id for m in match_context_rules(list(enumerate(content.splitlines(),1)),self.workflow)}
            self.assertNotIn("CRT-CI-066",found)
    def test_metadata(self):
        r=next(r for r in DEFAULT_CONTEXT_RULES if r.rule_id=="CRT-CI-066")
        self.assertEqual((r.category,r.kind,r.severity,r.confidence),("ci","policy","medium","high"))
if __name__ == "__main__": unittest.main()
