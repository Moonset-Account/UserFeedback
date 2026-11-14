"""CLI 入口测试：通过子进程真实运行 app.py。"""

import json
import os
import subprocess
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(REPO_ROOT, "app.py")
SAMPLES = os.path.join(REPO_ROOT, "samples")


class TestCli(unittest.TestCase):

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, APP, *args],
            capture_output=True, text=True, cwd=REPO_ROOT, timeout=30)

    def test_bundle_input_exit_zero_and_valid_json(self):
        bundle = os.path.join(SAMPLES, "case-01-closable", "input.json")
        proc = self.run_cli("--input", bundle)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(report["ticket"], "SEC-20261114-039")
        self.assertEqual(report["decisions"][0]["status"], "closable")
        self.assertEqual(report["decisions"][0]["status_label"], "可关闭")

    def test_pretty_output_still_valid_json(self):
        bundle = os.path.join(SAMPLES, "case-02-outdated-deployment", "input.json")
        proc = self.run_cli("--input", bundle, "--pretty")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(report["decisions"][0]["status"], "pending_deploy")

    def test_missing_input_file_exit_two(self):
        proc = self.run_cli("--input", "/nonexistent/bundle.json")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("输入错误", proc.stderr)

    def test_no_arguments_exit_two(self):
        proc = self.run_cli()
        self.assertEqual(proc.returncode, 2)


if __name__ == "__main__":
    unittest.main()
