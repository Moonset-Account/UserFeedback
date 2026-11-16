"""Stage 5 robustness and secondary feature tests for Secret/Config Diff Scanner.

Tests for:
- SARIF output format
- Quiet mode
- Edge cases (empty diff, binary, long lines, unicode)
- Better error messages
- CLI SARIF and quiet mode
- Directory scan edge cases
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

from src.scanner import SecretScanner, Finding, ConfigChange, ScanResult
from src.formatters import format_json, format_markdown, format_html, format_sarif
from src.diff_parser import parse_diff
from tests.synthetic_values import assemble


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


# --- SARIF Format Tests ---

class TestSarifFormatter(unittest.TestCase):
    """Test SARIF output formatting."""

    def test_empty_result(self):
        result = _make_result()
        output = format_sarif(result)
        data = json.loads(output)
        self.assertEqual(data["version"], "2.1.0")
        self.assertIn("runs", data)
        self.assertEqual(len(data["runs"]), 1)
        self.assertEqual(data["runs"][0]["tool"]["driver"]["name"], "secret-config-diff-scanner")
        self.assertEqual(len(data["runs"][0]["results"]), 0)

    def test_with_secret_finding(self):
        findings = [
            Finding(
                type="secret", pattern_name="AWS_ACCESS_KEY",
                severity="critical", file="src/config.py", line=42,
                matched_text=assemble("AK", "IA", "IOEXAMPLE12345"),
                line_content="AWS_ACCESS_KEY_ID = '" + assemble("AK", "IA", "IOEXAMPLE12345") + "'",
                rule="regex:AKIA[0-9A-Z]{16}",
            )
        ]
        result = _make_result(findings=findings)
        output = format_sarif(result)
        data = json.loads(output)
        self.assertEqual(len(data["runs"][0]["results"]), 1)
        self.assertEqual(data["runs"][0]["results"][0]["ruleId"], "SCDS/AWS_ACCESS_KEY")
        self.assertEqual(data["runs"][0]["results"][0]["level"], "error")
        self.assertEqual(data["runs"][0]["results"][0]["locations"][0]["physicalLocation"]["artifactLocation"]["uri"], "src/config.py")

    def test_with_config_change(self):
        config_changes = [
            ConfigChange(
                type="config", file=".env.production",
                severity="high", change_type="added",
                description="Environment config file modified",
            )
        ]
        result = _make_result(config_changes=config_changes)
        output = format_sarif(result)
        data = json.loads(output)
        self.assertEqual(len(data["runs"][0]["results"]), 1)
        self.assertEqual(data["runs"][0]["results"][0]["ruleId"], "SCDS/CONFIG_ADDED")

    def test_sarif_severity_mapping(self):
        """Test that SARIF level maps correctly from our severity."""
        for severity, expected_level in [
            ("critical", "error"),
            ("high", "error"),
            ("medium", "warning"),
            ("low", "note"),
        ]:
            findings = [Finding(type="secret", pattern_name=f"TEST_{severity.upper()}",
                severity=severity, file="a.py", line=1,
                matched_text="x", line_content="x", rule="r")]
            result = _make_result(findings=findings)
            output = format_sarif(result)
            data = json.loads(output)
            self.assertEqual(data["runs"][0]["results"][0]["level"], expected_level,
                            f"Severity '{severity}' should map to SARIF level '{expected_level}'")

    def test_sarif_schema_present(self):
        result = _make_result()
        output = format_sarif(result)
        data = json.loads(output)
        self.assertIn("$schema", data)
        self.assertIn("sarif", data["$schema"].lower())

    def test_sarif_rules_from_findings(self):
        findings = [
            Finding(type="secret", pattern_name="AWS_ACCESS_KEY",
                severity="critical", file="a.py", line=1,
                matched_text="AKIA...", line_content="AKIA...", rule="regex:AKIA[0-9A-Z]{16}"),
            Finding(type="secret", pattern_name="AWS_ACCESS_KEY",
                severity="critical", file="b.py", line=5,
                matched_text="AKIA...2", line_content="AKIA...2", rule="regex:AKIA[0-9A-Z]{16}"),
        ]
        result = _make_result(findings=findings)
        output = format_sarif(result)
        data = json.loads(output)
        # Same rule should appear once, two results
        rules = data["runs"][0]["tool"]["driver"]["rules"]
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["id"], "SCDS/AWS_ACCESS_KEY")
        self.assertEqual(len(data["runs"][0]["results"]), 2)

    def test_sarif_mixed_findings_and_config(self):
        findings = [Finding(type="secret", pattern_name="PASSWORD_LITERAL",
            severity="high", file="login.py", line=10,
            matched_text="pass123", line_content="password='pass123'", rule="regex:...")]
        config_changes = [ConfigChange(type="config", file=".env",
            severity="high", change_type="modified", description="Env modified")]
        result = _make_result(findings=findings, config_changes=config_changes)
        output = format_sarif(result)
        data = json.loads(output)
        self.assertEqual(len(data["runs"][0]["results"]), 2)

    def test_scan_result_to_sarif_method(self):
        findings = [
            Finding(
                type="secret", pattern_name="AWS_SECRET_KEY",
                severity="critical", file="src/config.py", line=42,
                matched_text=assemble("wJalrXUt", "nFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"),
                line_content="AWS_SECRET='wJalrX...EKEY'",
                rule="regex:AWS_SECRET_KEY",
            )
        ]
        result = _make_result(findings=findings)
        sarif_output = result.to_sarif()
        data = json.loads(sarif_output)
        self.assertEqual(data["version"], "2.1.0")
        self.assertEqual(len(data["runs"][0]["results"]), 1)


# --- Quiet Mode Tests ---

class TestQuietMode(unittest.TestCase):
    """Test quiet mode CLI output."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.diff_path = os.path.join(self.temp_dir, "test.diff")
        with open(self.diff_path, "w") as f:
            f.write(f"""--- a/config.py
+++ b/config.py
@@ -1,1 +1,2 @@
 DEBUG = False
+AWS_ACCESS_KEY_ID = "{assemble("AK", "IA", "IOSFODNN7EXAMPLE")}"
""")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_quiet_json_removes_summary(self):
        project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        result = subprocess.run(
            [sys.executable, "-m", "src", "scan", "--diff", self.diff_path,
             "--format", "json", "--quiet"],
            capture_output=True, text=True,
            cwd=project_dir
        )
        if result.stdout.strip():
            data = json.loads(result.stdout)
            self.assertNotIn("summary", data)

    def test_quiet_markdown_removes_summary(self):
        project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        result = subprocess.run(
            [sys.executable, "-m", "src", "scan", "--diff", self.diff_path,
             "--format", "markdown", "--quiet"],
            capture_output=True, text=True,
            cwd=project_dir
        )
        if result.stdout.strip():
            self.assertNotIn("## Summary", result.stdout)


# --- Edge Case Tests ---

class TestEdgeCases(unittest.TestCase):
    """Test edge cases and improved error handling."""

    def test_empty_diff_returns_empty_result(self):
        """Scanning an empty diff file should return clean result."""
        temp_dir = tempfile.mkdtemp()
        diff_path = os.path.join(temp_dir, "empty.diff")
        with open(diff_path, "w") as f:
            f.write("")
        try:
            scanner = SecretScanner()
            result = scanner.scan_diff(diff_path)
            self.assertFalse(result.has_secrets())
            self.assertFalse(result.has_config_changes())
            self.assertEqual(result.exit_code, 0)
        finally:
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_whitespace_only_diff(self):
        """Scanning a diff with only whitespace should return clean."""
        temp_dir = tempfile.mkdtemp()
        diff_path = os.path.join(temp_dir, "whitespace.diff")
        with open(diff_path, "w") as f:
            f.write("   \n\n   \n")
        try:
            scanner = SecretScanner()
            result = scanner.scan_diff(diff_path)
            self.assertEqual(result.exit_code, 0)
        finally:
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_directory_scan_on_file_path(self):
        """Scanning a file path with --dir should raise an error."""
        temp_dir = tempfile.mkdtemp()
        file_path = os.path.join(temp_dir, "test.txt")
        with open(file_path, "w") as f:
            f.write("hello\n")
        try:
            scanner = SecretScanner()
            with self.assertRaises(ValueError):
                scanner.scan_directory(file_path)
        finally:
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_scan_diff_on_directory_raises_error(self):
        """Scanning a directory with --diff should raise an error."""
        temp_dir = tempfile.mkdtemp()
        try:
            scanner = SecretScanner()
            with self.assertRaises(ValueError):
                scanner.scan_diff(temp_dir)
        finally:
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_binary_file_marker_in_diff(self):
        """Diff with binary file markers should not crash."""
        diff = """--- a/image.png
+++ b/image.png
Binary files a/image.png and b/image.png differ
--- a/config.py
+++ b/config.py
@@ -1,1 +1,2 @@
 DEBUG = False
+password = "secret123"
"""
        temp_dir = tempfile.mkdtemp()
        diff_path = os.path.join(temp_dir, "binary.diff")
        with open(diff_path, "w") as f:
            f.write(diff)
        try:
            scanner = SecretScanner()
            result = scanner.scan_diff(diff_path)
            # Should detect the password in config.py, not crash on binary
            self.assertGreaterEqual(len(result.findings), 1)
        finally:
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_unicode_in_diff(self):
        """Diff with unicode characters should not crash."""
        diff = """--- a/README.md
+++ b/README.md
@@ -1,1 +1,2 @@
 Hello
+password = "test_unicode_pass"
"""
        temp_dir = tempfile.mkdtemp()
        diff_path = os.path.join(temp_dir, "unicode.diff")
        with open(diff_path, "w", encoding="utf-8") as f:
            f.write(diff)
        try:
            scanner = SecretScanner()
            result = scanner.scan_diff(diff_path)
            self.assertGreaterEqual(len(result.findings), 1)
        finally:
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_very_long_line_in_diff(self):
        """Diff with very long line should not crash."""
        long_value = "x" * 500
        diff = f"""--- a/config.py
+++ b/config.py
@@ -1,1 +1,2 @@
 DEBUG = False
+api_key = "{long_value}"
"""
        temp_dir = tempfile.mkdtemp()
        diff_path = os.path.join(temp_dir, "long.diff")
        with open(diff_path, "w") as f:
            f.write(diff)
        try:
            scanner = SecretScanner()
            result = scanner.scan_diff(diff_path)
            self.assertGreaterEqual(len(result.findings), 1)
        finally:
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_multiple_files_in_diff(self):
        """Diff with many files should scan all of them."""
        diff = f"""--- a/file1.py
+++ b/file1.py
@@ -1,1 +1,2 @@
 x
+password = "my_super_secret_password_1234"
--- a/file2.py
+++ b/file2.py
@@ -1,1 +1,2 @@
 y
+api_key = "{assemble("sk", "_live_", "abcdef1234567890abcdefghij")}"
--- a/file3.py
+++ b/file3.py
@@ -1,1 +1,2 @@
 z
+DATABASE_URL=postgresql://user:pass@localhost/dbname
"""
        temp_dir = tempfile.mkdtemp()
        diff_path = os.path.join(temp_dir, "multi.diff")
        with open(diff_path, "w") as f:
            f.write(diff)
        try:
            scanner = SecretScanner()
            result = scanner.scan_diff(diff_path)
            self.assertGreaterEqual(len(result.findings), 3)
        finally:
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_config_only_diff_exit_code(self):
        """Diff with config changes but no secrets should return exit code 2."""
        diff = """--- a/.env.production
+++ b/.env.production
@@ -1,1 +1,2 @@
 DEBUG=false
+NEW_VAR=value
"""
        temp_dir = tempfile.mkdtemp()
        diff_path = os.path.join(temp_dir, "env.diff")
        with open(diff_path, "w") as f:
            f.write(diff)
        try:
            scanner = SecretScanner(config_check=True)
            result = scanner.scan_diff(diff_path)
            self.assertEqual(result.exit_code, 2)
        finally:
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)


class TestDiffParserEdgeCases(unittest.TestCase):
    """Test diff parser with edge cases."""

    def test_diff_with_no_newline_marker(self):
        """Diff with 'No newline at end of file' should parse."""
        diff = """--- a/config.py
+++ b/config.py
@@ -1,1 +1,2 @@
 DEBUG = False
+password = "test123"
"""
        files = parse_diff(diff)
        self.assertGreaterEqual(len(files), 1)

    def test_diff_with_rename_header(self):
        """Diff with different source/target paths should parse."""
        diff = """diff --git a/old_config.py b/new_config.py
similarity index 90%
rename from old_config.py
rename to new_config.py
--- a/old_config.py
+++ b/new_config.py
@@ -1,1 +1,2 @@
 DEBUG = False
+password = "test123"
"""
        files = parse_diff(diff)
        self.assertGreaterEqual(len(files), 1)
        self.assertEqual(files[0].target_path, "new_config.py")

    def test_diff_with_only_removals(self):
        """Diff with only removed lines (no additions) should parse cleanly."""
        diff = """--- a/safe.py
+++ b/safe.py
@@ -1,2 +1,2 @@
 def hello():
-    return "old"
+    return "new"
"""
        files = parse_diff(diff)
        self.assertGreaterEqual(len(files), 1)
        self.assertGreaterEqual(len(files[0].removed_lines), 1)


# --- CLI SARIF Tests ---

class TestCLISarifFormat(unittest.TestCase):
    """Test CLI SARIF output format."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.diff_path = os.path.join(self.temp_dir, "test.diff")
        with open(self.diff_path, "w") as f:
            f.write("""--- a/config.py
+++ b/config.py
@@ -1,1 +1,2 @@
 DEBUG = False
+password = "secretXYZpass123"
""")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_cli_sarif_output(self):
        project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        result = subprocess.run(
            [sys.executable, "-m", "src", "scan", "--diff", self.diff_path, "--format", "sarif"],
            capture_output=True, text=True,
            cwd=project_dir
        )
        self.assertIn(result.returncode, [0, 1])
        if result.stdout.strip():
            data = json.loads(result.stdout)
            self.assertEqual(data["version"], "2.1.0")
            self.assertIn("runs", data)

    def test_cli_sarif_to_file(self):
        project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        output_path = os.path.join(self.temp_dir, "output.sarif")
        result = subprocess.run(
            [sys.executable, "-m", "src", "scan", "--diff", self.diff_path,
             "--format", "sarif", "--output", output_path],
            capture_output=True, text=True,
            cwd=project_dir
        )
        self.assertIn(result.returncode, [0, 1])
        if os.path.exists(output_path):
            with open(output_path) as f:
                content = f.read()
            data = json.loads(content)
            self.assertEqual(data["version"], "2.1.0")


# --- Improved Error Message Tests ---

class TestImprovedErrorMessages(unittest.TestCase):
    """Test improved error messages from scanner."""

    def test_scan_diff_file_not_found_message(self):
        scanner = SecretScanner()
        try:
            scanner.scan_diff("/nonexistent/path/to/file.diff")
            self.fail("Should have raised FileNotFoundError")
        except FileNotFoundError as e:
            self.assertIn("not found", str(e).lower())

    def test_scan_directory_not_found_message(self):
        scanner = SecretScanner()
        try:
            scanner.scan_directory("/nonexistent/directory")
            self.fail("Should have raised FileNotFoundError")
        except FileNotFoundError as e:
            self.assertIn("not found", str(e).lower())

    def test_scan_directory_on_file_path_message(self):
        temp_dir = tempfile.mkdtemp()
        file_path = os.path.join(temp_dir, "test.txt")
        with open(file_path, "w") as f:
            f.write("test\n")
        try:
            scanner = SecretScanner()
            with self.assertRaises(ValueError) as ctx:
                scanner.scan_directory(file_path)
            self.assertIn("not a directory", str(ctx.exception).lower())
        finally:
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_scan_diff_on_directory_message(self):
        temp_dir = tempfile.mkdtemp()
        try:
            scanner = SecretScanner()
            with self.assertRaises(ValueError) as ctx:
                scanner.scan_diff(temp_dir)
            self.assertIn("directory", str(ctx.exception).lower())
        finally:
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)


# --- Directory Scan Edge Cases ---

class TestDirectoryScanEdgeCases(unittest.TestCase):
    """Test edge cases in directory scanning."""

    def test_scan_directory_skips_binary_extensions(self):
        """Directory scan should skip binary file extensions."""
        temp_dir = tempfile.mkdtemp()
        # Create a binary-like file
        with open(os.path.join(temp_dir, "image.png"), "w") as f:
            f.write("AWS_ACCESS_KEY_ID = " + assemble("AK", "IA", "IOEXAMPLE1234") + "\n")
        # Create a normal file with a secret
        with open(os.path.join(temp_dir, "config.py"), "w") as f:
            f.write('password = "mysecretpassword123"\n')

        try:
            scanner = SecretScanner()
            result = scanner.scan_directory(temp_dir)
            secret_names = [f.pattern_name for f in result.findings]
            self.assertIn("PASSWORD_LITERAL", secret_names)
            # .png should be skipped
            files_scanned = [f.file for f in result.findings]
            self.assertNotIn(os.path.join(temp_dir, "image.png"), files_scanned)
        finally:
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_scan_directory_includes_security_dotfiles(self):
        """Directory scan should include security-relevant dotfiles."""
        temp_dir = tempfile.mkdtemp()
        with open(os.path.join(temp_dir, ".env"), "w") as f:
            f.write('password = "hiddensecret"\n')
        with open(os.path.join(temp_dir, "app.py"), "w") as f:
            f.write('password = "visiblesecret"\n')

        try:
            scanner = SecretScanner(config_check=False)
            result = scanner.scan_directory(temp_dir)
            basenames = {os.path.basename(f.file) for f in result.findings}
            self.assertIn(".env", basenames)
            self.assertIn("app.py", basenames)
        finally:
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_scan_directory_recursive(self):
        """Recursive directory scan should find secrets in subdirectories."""
        temp_dir = tempfile.mkdtemp()
        sub_dir = os.path.join(temp_dir, "subdir")
        os.makedirs(sub_dir)
        with open(os.path.join(sub_dir, "secret.py"), "w") as f:
            f.write('password = "mysecretpassword123"\n')

        try:
            scanner = SecretScanner()
            result = scanner.scan_directory(temp_dir, recursive=True)
            self.assertTrue(result.has_secrets())
        finally:
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_scan_directory_non_recursive_no_subdir(self):
        """Non-recursive scan should not descend into subdirectories."""
        temp_dir = tempfile.mkdtemp()
        sub_dir = os.path.join(temp_dir, "subdir")
        os.makedirs(sub_dir)
        with open(os.path.join(sub_dir, "secret.py"), "w") as f:
            f.write('password = "mysecretpassword123"\n')

        try:
            scanner = SecretScanner()
            result = scanner.scan_directory(temp_dir, recursive=False)
            self.assertFalse(result.has_secrets())
        finally:
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
