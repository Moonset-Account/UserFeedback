import json
import unittest

from src.formatters import format_github, format_html, format_json, format_markdown, format_sarif
from src.scanner import Finding, ScanResult


class OutputPrivacyTests(unittest.TestCase):
    def test_all_report_formats_redact_secret_and_line(self):
        sentinel = "CRT_SYNTHETIC_SECRET_SENTINEL_9f2a"
        finding = Finding(
            type="secret", pattern_name="TEST_SECRET", severity="high",
            file="config.py", line=7, matched_text=sentinel,
            line_content=f"TOKEN={sentinel}", rule="secret-pattern", rule_id="CRT-SEC-001",
        )
        result = ScanResult(
            scanner="secret-config-diff-scanner", version="test", timestamp="2026-01-01T00:00:00Z",
            input_type="diff", input_source="synthetic.diff", findings=[finding],
        )
        outputs = [format_json(result), format_markdown(result), format_html(result), format_github(result), format_sarif(result)]
        for output in outputs:
            self.assertNotIn(sentinel, output)
        data = json.loads(outputs[0])
        self.assertEqual(data["findings"][0]["matched_text"], "[REDACTED]")
        self.assertEqual(data["findings"][0]["line_content"], "[REDACTED]")


if __name__ == "__main__":
    unittest.main()
