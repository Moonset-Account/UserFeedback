import json
import tempfile
import unittest
from pathlib import Path

from src.baseline import load_baseline, write_baseline
from src.formatters import format_github, format_html, format_json, format_markdown, format_sarif
from src.scanner import ConfigChange, Finding, ScanResult


class Stage7ContractFreezeTests(unittest.TestCase):
    def finding(self, **overrides):
        values = dict(
            type="secret", pattern_name="FIXTURE_PATTERN", severity="high", file="src/app.py", line=7,
            matched_text="fixture-value", line_content="fixture-value",
            rule="secret-pattern", rule_id="CRT-SEC-999", identity_path="src/app.py",
        )
        values.update(overrides)
        return Finding(**values)

    def result(self, findings=None, config_changes=None, **kwargs):
        return ScanResult("secret-config-diff-scanner", "3.0.1", "2026-07-19T00:00:00+00:00", "diff", "test.diff",
                          findings=findings or [], config_changes=config_changes or [], **kwargs)

    def test_known_finding_v1_fingerprint(self):
        self.assertEqual(self.finding().fingerprint, "sha256:55673dcac779e8435aa3b561f881137f4b6ab79eadcb2194be7dd62a35938d05")

    def test_identity_path_and_windows_normalization(self):
        self.assertEqual(self.finding(file="other.py", identity_path="src/app.py").fingerprint, self.finding().fingerprint)
        self.assertEqual(self.finding(file="src\\app.py", identity_path=None).fingerprint, self.finding().fingerprint)

    def test_v1_is_stable_for_line_but_changes_for_rule_and_path(self):
        base = self.finding().fingerprint
        self.assertEqual(base, self.finding(line=8).fingerprint)
        self.assertNotEqual(base, self.finding(rule_id="CRT-SEC-998").fingerprint)
        self.assertNotEqual(base, self.finding(identity_path="src/other.py").fingerprint)

    def test_existing_json_contract_and_redaction(self):
        data = json.loads(format_json(self.result([self.finding()])))
        self.assertEqual({"scanner", "version", "timestamp", "input_type", "input_source", "summary", "findings", "config_changes"}, set(data))
        item = data["findings"][0]
        self.assertEqual(item["rule_id"], "CRT-SEC-999")
        self.assertEqual(item["matched_text"], "[REDACTED]")
        self.assertNotIn("super-secret-value", json.dumps(data))

    def test_existing_sarif_rule_and_partial_fingerprint(self):
        data = json.loads(format_sarif(self.result([self.finding()])))
        result = data["runs"][0]["results"][0]
        self.assertEqual(result["ruleId"], "CRT-SEC-999")
        self.assertEqual(result["properties"]["fingerprint"], self.finding().fingerprint)

    def test_existing_renderer_redaction(self):
        result = self.result([self.finding()])
        for output in (format_markdown(result), format_html(result), format_github(result)):
            self.assertNotIn("super-secret-value", output)
            self.assertIn("CRT-SEC-999", output)

    def test_summary_and_exit_codes(self):
        self.assertEqual(self.result().summary["total_findings"], 0)
        self.assertEqual(self.result().exit_code, 0)
        self.assertEqual(self.result([self.finding()]).exit_code, 1)
        config = ConfigChange("config", ".env", "high", "modified", "config changed")
        self.assertEqual(self.result(config_changes=[config]).exit_code, 2)

    def test_baseline_matched_stale_and_suppression(self):
        finding = self.finding()
        stale = self.finding(rule_id="CRT-SEC-998").fingerprint
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "baseline.json"
            write_baseline(str(path), [finding.fingerprint, stale])
            self.assertEqual(load_baseline(str(path)), {finding.fingerprint, stale})
        result = self.result([finding])
        result.baseline_total = 2
        result.baseline_matched = 1
        result.baseline_stale = 1
        self.assertEqual(result.summary["baseline_matched"], 1)
        self.assertEqual(result.summary["baseline_stale"], 1)

    def test_order_is_preserved_for_existing_findings(self):
        first = self.finding(rule_id="CRT-SEC-001")
        second = self.finding(rule_id="CRT-SEC-002")
        data = json.loads(format_json(self.result([first, second])))
        self.assertEqual([x["rule_id"] for x in data["findings"]], ["CRT-SEC-001", "CRT-SEC-002"])


if __name__ == "__main__":
    unittest.main()
