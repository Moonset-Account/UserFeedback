"""Unit tests for allowlist parsing and matching."""

import unittest
from src.allowlist import AllowlistRule, parse_allowlist, is_suppressed, load_allowlist


class TestAllowlistParsing(unittest.TestCase):
    """Test allowlist rule parsing."""

    def test_parse_empty_allowlist(self):
        rules = parse_allowlist("")
        self.assertEqual(len(rules), 0)

    def test_parse_comments_only(self):
        text = "# Comment line\n# Another comment\n"
        rules = parse_allowlist(text)
        self.assertEqual(len(rules), 0)

    def test_parse_pattern_rule(self):
        text = "pattern:AWS_ACCESS_KEY"
        rules = parse_allowlist(text)
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0].rule_type, "pattern")
        self.assertEqual(rules[0].pattern, "AWS_ACCESS_KEY")

    def test_parse_pattern_with_path(self):
        text = "pattern:PASSWORD_LITERAL path:test_*.py"
        rules = parse_allowlist(text)
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0].pattern, "PASSWORD_LITERAL")
        self.assertEqual(rules[0].path, "test_*.py")

    def test_parse_pattern_with_severity(self):
        text = "pattern:BASE64_SECRET severity:low"
        rules = parse_allowlist(text)
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0].pattern, "BASE64_SECRET")
        self.assertEqual(rules[0].severity, "low")

    def test_parse_value_rule(self):
        text = "value:test_password_123"
        rules = parse_allowlist(text)
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0].rule_type, "value")
        self.assertEqual(rules[0].value, "test_password_123")

    def test_parse_path_rule(self):
        text = "path:tests/**"
        rules = parse_allowlist(text)
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0].rule_type, "path")
        self.assertEqual(rules[0].path, "tests/**")

    def test_parse_multiple_rules(self):
        text = """# Comment
pattern:AWS_ACCESS_KEY
value:test_token_abc
path:tests/**
pattern:PASSWORD_LITERAL severity:low"""
        rules = parse_allowlist(text)
        self.assertEqual(len(rules), 4)

    def test_parse_blank_lines_skipped(self):
        text = "pattern:AWS_ACCESS_KEY\n\n\nvalue:test_token"
        rules = parse_allowlist(text)
        self.assertEqual(len(rules), 2)


class TestAllowlistSuppression(unittest.TestCase):
    """Test that allowlist rules correctly suppress findings."""

    def test_suppress_by_pattern_name(self):
        rules = [AllowlistRule(rule_type="pattern", pattern="AWS_ACCESS_KEY")]
        self.assertTrue(is_suppressed("AWS_ACCESS_KEY", "critical", "src/config.py", "AKIAIO...MPLE", rules))

    def test_does_not_suppress_different_pattern(self):
        rules = [AllowlistRule(rule_type="pattern", pattern="AWS_ACCESS_KEY")]
        self.assertFalse(is_suppressed("GITHUB_TOKEN", "critical", "src/config.py", "ghp_abc...", rules))

    def test_suppress_by_value(self):
        rules = [AllowlistRule(rule_type="value", value="test_password_123")]
        self.assertTrue(is_suppressed("PASSWORD_LITERAL", "high", "test_login.py", "password=test_password_123", rules))

    def test_suppress_by_path_glob(self):
        rules = [AllowlistRule(rule_type="path", path="tests/**")]
        self.assertTrue(is_suppressed("PASSWORD_LITERAL", "high", "tests/test_login.py", "password=abc", rules))

    def test_does_not_suppress_different_path(self):
        rules = [AllowlistRule(rule_type="path", path="tests/**")]
        self.assertFalse(is_suppressed("PASSWORD_LITERAL", "high", "src/login.py", "password=abc", rules))

    def test_suppress_pattern_with_path(self):
        rules = [AllowlistRule(rule_type="pattern", pattern="PASSWORD_LITERAL", path="test_*.py")]
        self.assertTrue(is_suppressed("PASSWORD_LITERAL", "high", "test_login.py", "password=abc", rules))
        self.assertFalse(is_suppressed("PASSWORD_LITERAL", "high", "src/login.py", "password=abc", rules))

    def test_suppress_pattern_with_severity(self):
        rules = [AllowlistRule(rule_type="pattern", pattern="BASE64_SECRET", severity="low")]
        self.assertTrue(is_suppressed("BASE64_SECRET", "low", "src/config.py", "abc123...", rules))
        self.assertFalse(is_suppressed("BASE64_SECRET", "high", "src/config.py", "abc123...", rules))

    def test_no_rules_means_no_suppression(self):
        self.assertFalse(is_suppressed("AWS_ACCESS_KEY", "critical", "src/config.py", "AKIAIO...MPLE", []))

    def test_suppress_by_path_double_star(self):
        rules = [AllowlistRule(rule_type="path", path="examples/**")]
        self.assertTrue(is_suppressed("API_KEY_LITERAL", "high", "examples/sample.py", "api_key=abc", rules))
        self.assertFalse(is_suppressed("API_KEY_LITERAL", "high", "src/app.py", "api_key=abc", rules))


class TestLoadAllowlist(unittest.TestCase):
    """Test loading allowlist from file."""

    def test_load_nonexistent_file(self):
        rules = load_allowlist("/nonexistent/path/.secretsallowlist")
        self.assertEqual(len(rules), 0)


if __name__ == "__main__":
    unittest.main()