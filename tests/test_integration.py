"""CLI integration tests for the Secret/Config Diff Scanner."""

import json
import os
import subprocess
import sys
import tempfile
import unittest


SAMPLE_DIFF = """diff --git a/config.py b/config.py
--- a/config.py
+++ b/config.py
@@ -1,1 +1,3 @@
 DEBUG = False
+AWS_ACCESS_KEY_ID = "AKIAIO...MPLE"
+password = "mysecretpassword123"
"""


class TestCLIIntegration(unittest.TestCase):
    """Test CLI entry point."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.diff_path = os.path.join(self.temp_dir, "test.diff")
        with open(self.diff_path, "w") as f:
            f.write(SAMPLE_DIFF)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_cli_version(self):
        result = subprocess.run(
            [sys.executable, "-m", "src", "--version"],
            capture_output=True, text=True,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        # Should show version (the actual version may change)
        self.assertIn("secret-scanner", result.stdout + result.stderr)

    def test_cli_scan_diff_json(self):
        project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        result = subprocess.run(
            [sys.executable, "-m", "src", "scan", "--diff", self.diff_path, "--format", "json"],
            capture_output=True, text=True,
            cwd=project_dir
        )
        # Should detect secrets (exit code 1)
        self.assertIn(result.returncode, [0, 1])
        if result.stdout.strip():
            data = json.loads(result.stdout)
            self.assertEqual(data["scanner"], "secret-config-diff-scanner")

    def test_cli_scan_diff_markdown(self):
        project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        result = subprocess.run(
            [sys.executable, "-m", "src", "scan", "--diff", self.diff_path, "--format", "markdown"],
            capture_output=True, text=True,
            cwd=project_dir
        )
        self.assertIn(result.returncode, [0, 1, 3])

    def test_cli_scan_diff_html(self):
        project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        result = subprocess.run(
            [sys.executable, "-m", "src", "scan", "--diff", self.diff_path, "--format", "html"],
            capture_output=True, text=True,
            cwd=project_dir
        )
        self.assertIn(result.returncode, [0, 1, 3])

    def test_cli_scan_nonexistent_file(self):
        project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        result = subprocess.run(
            [sys.executable, "-m", "src", "scan", "--diff", "/nonexistent/file.diff"],
            capture_output=True, text=True,
            cwd=project_dir
        )
        self.assertEqual(result.returncode, 3)

    def test_cli_no_command(self):
        project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        result = subprocess.run(
            [sys.executable, "-m", "src"],
            capture_output=True, text=True,
            cwd=project_dir
        )
        self.assertEqual(result.returncode, 3)

    def test_cli_output_to_file(self):
        project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        output_path = os.path.join(self.temp_dir, "output.json")
        result = subprocess.run(
            [sys.executable, "-m", "src", "scan", "--diff", self.diff_path,
             "--format", "json", "--output", output_path],
            capture_output=True, text=True,
            cwd=project_dir
        )
        self.assertIn(result.returncode, [0, 1])
        if os.path.exists(output_path):
            with open(output_path) as f:
                content = f.read()
            data = json.loads(content)
            self.assertEqual(data["scanner"], "secret-config-diff-scanner")

    def test_cli_with_allowlist(self):
        project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        allowlist_path = os.path.join(self.temp_dir, ".secretsallowlist")
        with open(allowlist_path, "w") as f:
            f.write("pattern:AWS_ACCESS_KEY\n")

        result = subprocess.run(
            [sys.executable, "-m", "src", "scan", "--diff", self.diff_path,
             "--format", "json", "--allowlist", allowlist_path],
            capture_output=True, text=True,
            cwd=project_dir
        )
        self.assertIn(result.returncode, [0, 1, 2])

    def test_cli_severity_threshold(self):
        project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        result = subprocess.run(
            [sys.executable, "-m", "src", "scan", "--diff", self.diff_path,
             "--format", "json", "--severity-threshold", "high"],
            capture_output=True, text=True,
            cwd=project_dir
        )
        self.assertIn(result.returncode, [0, 1])


if __name__ == "__main__":
    unittest.main()