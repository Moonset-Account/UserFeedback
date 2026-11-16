"""Unit tests for output formatters."""

import json
import unittest
from datetime import datetime, timezone
from src.scanner import Finding, ConfigChange, ScanResult
from src.formatters import format_json, format_markdown, format_html


def _make_result(findings=None, config_changes=None) -> ScanResult:
    """Helper to create a ScanResult for testing."""
    return ScanResult(
        scanner="secret-config-diff-scanner",
        version="1.0.0",
        timestamp="2026-06-27T10:00:00+00:00",
        input_type="diff",
        input_source="test.diff",
        findings=findings or [],
        config_changes=config_changes or [],
    )


class TestJsonFormatter(unittest.TestCase):
    """Test JSON output formatting."""

    def test_empty_result(self):
        result = _make_result()
        output = format_json(result)
        data = json.loads(output)
        self.assertEqual(data["scanner"], "secret-config-diff-scanner")
        self.assertIn("version", data)
        self.assertEqual(data["summary"]["total_findings"], 0)

    def test_with_findings(self):
        findings = [
            Finding(
                type="secret",
                pattern_name="AWS_ACCESS_KEY",
                severity="critical",
                file="src/config.py",
                line=42,
                matched_text="AKIAIO...MPLE",
                line_content="AWS_ACCESS_KEY_ID = 'AKIAIO...MPLE'",
                rule="regex:AKIA[0-9A-Z]{16}",
            )
        ]
        result = _make_result(findings=findings)
        output = format_json(result)
        data = json.loads(output)
        self.assertEqual(len(data["findings"]), 1)
        self.assertEqual(data["findings"][0]["pattern_name"], "AWS_ACCESS_KEY")
        self.assertEqual(data["summary"]["total_findings"], 1)
        self.assertEqual(data["summary"]["critical"], 1)

    def test_with_config_changes(self):
        config_changes = [
            ConfigChange(
                type="config",
                file=".env.production",
                severity="high",
                change_type="modified",
                description="Environment config file modified",
            )
        ]
        result = _make_result(config_changes=config_changes)
        output = format_json(result)
        data = json.loads(output)
        self.assertEqual(len(data["config_changes"]), 1)
        self.assertEqual(data["summary"]["config_findings"], 1)

    def test_json_is_valid(self):
        findings = [
            Finding(
                type="secret", pattern_name="PASSWORD_LITERAL",
                severity="high", file="login.py", line=10,
                matched_text="password123", line_content="password = 'password123'",
                rule="regex:...",
            )
        ]
        result = _make_result(findings=findings)
        output = format_json(result)
        # Should parse without error
        data = json.loads(output)
        self.assertIsInstance(data, dict)


class TestMarkdownFormatter(unittest.TestCase):
    """Test Markdown output formatting."""

    def test_empty_result(self):
        result = _make_result()
        output = format_markdown(result)
        self.assertIn("# Secret/Config Diff Scan Report", output)
        self.assertIn("Total findings | 0", output)

    def test_with_findings(self):
        findings = [
            Finding(
                type="secret", pattern_name="AWS_ACCESS_KEY",
                severity="critical", file="src/config.py", line=42,
                matched_text="AKIAIO...MPLE",
                line_content="AWS_ACCESS_KEY_ID = 'AKIAIO...MPLE'",
                rule="regex:AKIA[0-9A-Z]{16}",
            )
        ]
        result = _make_result(findings=findings)
        output = format_markdown(result)
        self.assertIn("Secret Findings", output)
        self.assertIn("AWS_ACCESS_KEY", output)
        self.assertIn("critical", output)

    def test_with_config_changes(self):
        config_changes = [
            ConfigChange(
                type="config", file=".env.production",
                severity="high", change_type="modified",
                description="Environment config file modified",
            )
        ]
        result = _make_result(config_changes=config_changes)
        output = format_markdown(result)
        self.assertIn("Config Changes", output)
        self.assertIn(".env.production", output)

    def test_remediation_section(self):
        findings = [
            Finding(
                type="secret", pattern_name="PASSWORD_LITERAL",
                severity="high", file="login.py", line=10,
                matched_text="password123", line_content="password='password123'",
                rule="regex:...",
            )
        ]
        result = _make_result(findings=findings)
        output = format_markdown(result)
        self.assertIn("Remediation", output)


class TestHtmlFormatter(unittest.TestCase):
    """Test HTML output formatting."""

    def test_empty_result(self):
        result = _make_result()
        output = format_html(result)
        self.assertIn("<!DOCTYPE html>", output)
        self.assertIn("Secret/Config Diff Scan Report", output)

    def test_with_findings(self):
        findings = [
            Finding(
                type="secret", pattern_name="AWS_ACCESS_KEY",
                severity="critical", file="src/config.py", line=42,
                matched_text="AKIAIO...MPLE",
                line_content="AWS_ACCESS_KEY_ID = 'AKIAIO...MPLE'",
                rule="regex:AKIA[0-9A-Z]{16}",
            )
        ]
        result = _make_result(findings=findings)
        output = format_html(result)
        self.assertIn("AWS_ACCESS_KEY", output)
        self.assertIn("Secret Findings", output)

    def test_html_escapes_special_chars(self):
        findings = [
            Finding(
                type="secret", pattern_name="PASSWORD_LITERAL",
                severity="high", file="src/<script>.py", line=10,
                matched_text="pass&word<>123",
                line_content="password = 'pass&word<>123'",
                rule="regex:...",
            )
        ]
        result = _make_result(findings=findings)
        output = format_html(result)
        # HTML should escape special characters in file paths and matched text
        self.assertNotIn("<script>", output.split("</script>")[-1] if "</script>" in output else output)


class TestScanResultExitCode(unittest.TestCase):
    """Test ScanResult exit code logic."""

    def test_clean_exit_code(self):
        result = _make_result()
        self.assertEqual(result.exit_code, 0)

    def test_secret_exit_code(self):
        findings = [
            Finding(
                type="secret", pattern_name="AWS_ACCESS_KEY",
                severity="critical", file="src/config.py", line=42,
                matched_text="AKIAIO...MPLE",
                line_content="AWS_ACCESS_KEY_ID = 'AKIAIO...MPLE'",
                rule="regex:...",
            )
        ]
        result = _make_result(findings=findings)
        self.assertEqual(result.exit_code, 1)

    def test_config_only_exit_code(self):
        config_changes = [
            ConfigChange(
                type="config", file=".env.production",
                severity="high", change_type="modified",
                description="Environment config file modified",
            )
        ]
        result = _make_result(config_changes=config_changes)
        self.assertEqual(result.exit_code, 2)


class TestScanResultSummary(unittest.TestCase):
    """Test ScanResult summary computation."""

    def test_empty_summary(self):
        result = _make_result()
        s = result.summary
        self.assertEqual(s["total_findings"], 0)
        self.assertEqual(s["critical"], 0)
        self.assertEqual(s["high"], 0)
        self.assertEqual(s["medium"], 0)
        self.assertEqual(s["low"], 0)

    def test_summary_with_findings(self):
        findings = [
            Finding(type="secret", pattern_name="AWS_ACCESS_KEY",
                    severity="critical", file="a.py", line=1,
                    matched_text="x", line_content="x", rule="r"),
            Finding(type="secret", pattern_name="PASSWORD_LITERAL",
                    severity="high", file="b.py", line=2,
                    matched_text="y", line_content="y", rule="r"),
        ]
        result = _make_result(findings=findings)
        s = result.summary
        self.assertEqual(s["total_findings"], 2)
        self.assertEqual(s["critical"], 1)
        self.assertEqual(s["high"], 1)
        self.assertEqual(s["secret_findings"], 2)

    def test_summary_with_config_changes(self):
        config_changes = [
            ConfigChange(type="config", file=".env", severity="high",
                        change_type="modified", description="Env modified"),
            ConfigChange(type="config", file="docker-compose.yml", severity="medium",
                        change_type="modified", description="Infra modified"),
        ]
        result = _make_result(config_changes=config_changes)
        s = result.summary
        self.assertEqual(s["config_findings"], 2)
        self.assertEqual(s["high"], 1)
        self.assertEqual(s["medium"], 1)


if __name__ == "__main__":
    unittest.main()