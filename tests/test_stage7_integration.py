import json
import unittest

from src.scanner import ConfigChange, Finding, ScanResult


class Stage7IntegrationTests(unittest.TestCase):
    def finding(self, rule_id="CRT-SEC-999", line=7):
        return Finding("secret", "FIXTURE_PATTERN", "high", "src/app.py", line, "fixture-evidence", "fixture = fixture-evidence",
                       "secret-pattern", rule_id, category="secret", confidence="high", remediation="Rotate it.")

    def test_opt_in_pipeline_preserves_legacy_result(self):
        from src.stage7_pipeline import build_stage7_graph
        finding = self.finding()
        config = ConfigChange("config", ".env", "high", "modified", "review configuration")
        result = ScanResult("scanner", "3", "now", "diff", "memory", findings=[finding], config_changes=[config])
        before = (result.summary.copy(), result.exit_code, finding.fingerprint)
        graph = build_stage7_graph(result, scope="repo:test")
        after = (result.summary.copy(), result.exit_code, finding.fingerprint)
        self.assertEqual(before, after)
        self.assertEqual(len(graph.observations), 2)
        self.assertEqual(len(graph.facts), 2)
        self.assertEqual(len(graph.deltas), 2)
        self.assertEqual(len(graph.incidents), 2)

    def test_graph_is_deterministic_and_redacted(self):
        from src.stage7_pipeline import build_stage7_graph
        result = ScanResult("scanner", "3", "now", "diff", "memory", findings=[self.finding()])
        one = build_stage7_graph(result, scope="repo:test")
        two = build_stage7_graph(result, scope="repo:test")
        self.assertEqual(one.to_dict(), two.to_dict())
        text = json.dumps(one.to_dict())
        self.assertNotIn("fixture-evidence", text)
        self.assertNotIn("matched_text", text)
        self.assertNotIn("line_content", text)

    def test_external_adapter_requires_normalized_safe_observation(self):
        from src.stage7_pipeline import build_stage7_graph
        result = ScanResult("scanner", "3", "now", "diff", "memory")
        graph = build_stage7_graph(result, scope="repo:test", external_observations=[])
        self.assertEqual(graph.to_dict()["observations"], [])
        with self.assertRaises(ValueError):
            build_stage7_graph(result, scope="repo:test", external_observations=[{"raw": "secret"}])

    def test_no_automatic_public_output_or_baseline_change(self):
        from src.stage7_pipeline import build_stage7_graph
        from src.formatters import format_json
        result = ScanResult("scanner", "3", "now", "diff", "memory", findings=[self.finding()])
        public = json.loads(format_json(result))
        graph = build_stage7_graph(result, scope="repo:test")
        self.assertNotIn("observations", public)
        self.assertNotIn("facts", public)
        self.assertNotIn("incidents", public)
        self.assertNotIn("stage7", public)
        self.assertEqual(result.exit_code, 1)
        self.assertTrue(graph.incidents)


if __name__ == "__main__":
    unittest.main()
