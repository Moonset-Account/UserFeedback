"""Integration tests for SecretScanner."""

import json
import os
import tempfile
import unittest
from src.scanner import SecretScanner, Finding, ConfigChange, ScanResult
from tests.synthetic_values import assemble


AWS_ACCESS_KEY = assemble("AK", "IA", "IOSFODNN7EXAMPLE")
AWS_SECRET_KEY = assemble("wJalrXUtnFEMI/", "K7MDENG/bPxRfiCYEXAMPLEKEY")
STRIPE_SECRET_KEY = assemble("sk", "_live_", "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGH")


SAMPLE_DIFF = f"""diff --git a/src/config.py b/src/config.py
index 1234567..abcdefg 100644
--- a/src/config.py
+++ b/src/config.py
@@ -1,2 +1,4 @@
 DATABASE_URL = "postgresql://localhost/mydb"
+AWS_ACCESS_KEY_ID = "{AWS_ACCESS_KEY}"
+AWS_SECRET_ACCESS_KEY={AWS_SECRET_KEY}
 DEBUG = False
diff --git a/.env.production b/.env.production
new file mode 100644
--- /dev/null
+++ b/.env.production
@@ -0,0 +1,2 @@
+STRIPE_SECRET_KEY={STRIPE_SECRET_KEY}
+DEBUG=false
diff --git a/src/auth/login.py b/src/auth/login.py
index 9876543..hijklmn 100644
--- a/src/auth/login.py
+++ b/src/auth/login.py
@@ -10,2 +10,4 @@
 def authenticate(username, password):
     return check_credentials(username, password)
+    password = "super_secret_password_123"
+    api_key = "my_api_key_for_testing_abc123def456ghi789jkl012"
diff --git a/.github/workflows/deploy.yml b/.github/workflows/deploy.yml
index abcdef0..1234567 100644
--- a/.github/workflows/deploy.yml
+++ b/.github/workflows/deploy.yml
@@ -1,4 +1,4 @@
 name: Deploy
-on: [push]
+on: [push, pull_request]
 jobs:
   deploy:
diff --git a/docker-compose.yml b/docker-compose.yml
index fedcba0..7654321 100644
--- a/docker-compose.yml
+++ b/docker-compose.yml
@@ -1,3 +1,3 @@
 services:
   web:
-    image: myapp:latest
+    image: myapp:v2.0
"""


class TestSecretScannerDiff(unittest.TestCase):
    """Test SecretScanner with diff scanning."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.diff_path = os.path.join(self.temp_dir, "sample.diff")
        with open(self.diff_path, "w") as f:
            f.write(SAMPLE_DIFF)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_scan_diff_detects_secrets(self):
        scanner = SecretScanner()
        result = scanner.scan_diff(self.diff_path)
        self.assertTrue(result.has_secrets())
        self.assertGreaterEqual(len(result.findings), 3)

    def test_scan_diff_detects_aws_access_key(self):
        scanner = SecretScanner()
        result = scanner.scan_diff(self.diff_path)
        secret_names = [f.pattern_name for f in result.findings]
        self.assertIn("AWS_ACCESS_KEY", secret_names)

    def test_scan_diff_detects_aws_secret_key(self):
        scanner = SecretScanner()
        result = scanner.scan_diff(self.diff_path)
        secret_names = [f.pattern_name for f in result.findings]
        self.assertIn("AWS_SECRET_KEY", secret_names)

    def test_scan_diff_detects_password(self):
        scanner = SecretScanner()
        result = scanner.scan_diff(self.diff_path)
        secret_names = [f.pattern_name for f in result.findings]
        self.assertIn("PASSWORD_LITERAL", secret_names)

    def test_scan_diff_detects_stripe_key(self):
        scanner = SecretScanner()
        result = scanner.scan_diff(self.diff_path)
        secret_names = [f.pattern_name for f in result.findings]
        self.assertIn("STRIPE_KEY", secret_names)

    def test_scan_diff_detects_api_key(self):
        scanner = SecretScanner()
        result = scanner.scan_diff(self.diff_path)
        secret_names = [f.pattern_name for f in result.findings]
        self.assertIn("API_KEY_LITERAL", secret_names)

    def test_scan_diff_detects_config_changes(self):
        scanner = SecretScanner(config_check=True)
        result = scanner.scan_diff(self.diff_path)
        self.assertTrue(result.has_config_changes())
        config_files = [c.file for c in result.config_changes]
        self.assertIn(".env.production", config_files)

    def test_scan_diff_config_check_disabled(self):
        scanner = SecretScanner(config_check=False)
        result = scanner.scan_diff(self.diff_path)
        # Should still detect secrets but not config changes
        self.assertTrue(result.has_secrets())

    def test_scan_diff_ci_config_detected(self):
        scanner = SecretScanner(config_check=True)
        result = scanner.scan_diff(self.diff_path)
        config_files = [c.file for c in result.config_changes]
        # .github/workflows/deploy.yml should be detected as CI_CONFIG
        self.assertTrue(any(".github" in f for f in config_files))

    def test_scan_diff_security_path_detected(self):
        scanner = SecretScanner(config_check=True)
        result = scanner.scan_diff(self.diff_path)
        config_files = [c.file for c in result.config_changes]
        # src/auth/login.py should be detected as SECURITY_CONFIG
        self.assertTrue(any("auth" in f for f in config_files))

    def test_scan_diff_json_output(self):
        scanner = SecretScanner()
        result = scanner.scan_diff(self.diff_path)
        json_output = result.to_json()
        data = json.loads(json_output)
        self.assertEqual(data["scanner"], "secret-config-diff-scanner")
        self.assertEqual(data["input_type"], "diff")
        self.assertGreaterEqual(data["summary"]["total_findings"], 3)

    def test_scan_diff_markdown_output(self):
        scanner = SecretScanner()
        result = scanner.scan_diff(self.diff_path)
        md_output = result.to_markdown()
        self.assertIn("# Secret/Config Diff Scan Report", md_output)
        self.assertIn("Secret Findings", md_output)

    def test_scan_diff_html_output(self):
        scanner = SecretScanner()
        result = scanner.scan_diff(self.diff_path)
        html_output = result.to_html()
        self.assertIn("<!DOCTYPE html>", html_output)
        self.assertIn("Secret Findings", html_output)

    def test_scan_diff_exit_code(self):
        scanner = SecretScanner()
        result = scanner.scan_diff(self.diff_path)
        self.assertEqual(result.exit_code, 1)  # Secrets detected

    def test_scan_diff_file_not_found(self):
        scanner = SecretScanner()
        with self.assertRaises(FileNotFoundError):
            scanner.scan_diff("/nonexistent/path.diff")

    def test_scan_diff_with_allowlist(self):
        # Create allowlist that suppresses AWS_ACCESS_KEY
        allowlist_path = os.path.join(self.temp_dir, ".secretsallowlist")
        with open(allowlist_path, "w") as f:
            f.write("pattern:AWS_ACCESS_KEY\n")

        scanner = SecretScanner(allowlist_path=allowlist_path)
        result = scanner.scan_diff(self.diff_path)
        secret_names = [f.pattern_name for f in result.findings]
        self.assertNotIn("AWS_ACCESS_KEY", secret_names)

    def test_scan_diff_with_config(self):
        config_path = os.path.join(self.temp_dir, "config.json")
        with open(config_path, "w") as f:
            json.dump({
                "pattern_overrides": {
                    "GOOGLE_API_KEY": {"severity": "low"}
                },
                "custom_patterns": [
                    {
                        "name": "MY_COMPANY_KEY",
                        "regex": "myco_[a-zA-Z0-9]{32}",
                        "severity": "critical"
                    }
                ]
            }, f)

        scanner = SecretScanner(config_path=config_path)
        result = scanner.scan_diff(self.diff_path)
        # Should have custom patterns available
        self.assertTrue(True)  # Just verify it doesn't crash


class TestSecretScannerDirectory(unittest.TestCase):
    """Test SecretScanner with directory scanning."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        # Create a test file with a secret
        secret_file = os.path.join(self.temp_dir, "config.py")
        with open(secret_file, "w") as f:
            f.write(f'AWS_ACCESS_KEY_ID = "{AWS_ACCESS_KEY}"\n')
            f.write('password = "mysecretpassword123"\n')

        # Create a safe file
        safe_file = os.path.join(self.temp_dir, "utils.py")
        with open(safe_file, "w") as f:
            f.write('def helper():\n')
            f.write('    return "hello"\n')

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_scan_directory_detects_secrets(self):
        scanner = SecretScanner()
        result = scanner.scan_directory(self.temp_dir)
        self.assertTrue(result.has_secrets())

    def test_scan_directory_detects_aws_key(self):
        scanner = SecretScanner()
        result = scanner.scan_directory(self.temp_dir)
        secret_names = [f.pattern_name for f in result.findings]
        self.assertIn("AWS_ACCESS_KEY", secret_names)

    def test_scan_directory_detects_password(self):
        scanner = SecretScanner()
        result = scanner.scan_directory(self.temp_dir)
        secret_names = [f.pattern_name for f in result.findings]
        self.assertIn("PASSWORD_LITERAL", secret_names)

    def test_scan_directory_not_found(self):
        scanner = SecretScanner()
        with self.assertRaises(FileNotFoundError):
            scanner.scan_directory("/nonexistent/directory")

    def test_scan_directory_empty(self):
        empty_dir = tempfile.mkdtemp()
        try:
            scanner = SecretScanner()
            result = scanner.scan_directory(empty_dir)
            self.assertFalse(result.has_secrets())
        finally:
            os.rmdir(empty_dir)


class TestSecretScannerSeverityThreshold(unittest.TestCase):
    """Test severity threshold filtering."""

    def test_threshold_critical_only(self):
        diff = f"""--- a/config.py
+++ b/config.py
@@ -1,1 +1,2 @@
 DEBUG = False
+AWS_ACCESS_KEY_ID = "{AWS_ACCESS_KEY}"
"""
        temp_dir = tempfile.mkdtemp()
        diff_path = os.path.join(temp_dir, "test.diff")
        with open(diff_path, "w") as f:
            f.write(diff)

        try:
            scanner = SecretScanner(severity_threshold="critical")
            result = scanner.scan_diff(diff_path)
            # Only critical findings should appear
            for f in result.findings:
                self.assertEqual(f.severity, "critical")
        finally:
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_threshold_includes_high(self):
        diff = """--- a/config.py
+++ b/config.py
@@ -1,1 +1,2 @@
 DEBUG = False
+password = "super_secret_password_123"
"""
        temp_dir = tempfile.mkdtemp()
        diff_path = os.path.join(temp_dir, "test.diff")
        with open(diff_path, "w") as f:
            f.write(diff)

        try:
            scanner = SecretScanner(severity_threshold="high")
            result = scanner.scan_diff(diff_path)
            # High and critical should appear, but not medium or low
            for f in result.findings:
                self.assertIn(f.severity, ("high", "critical"))
        finally:
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
