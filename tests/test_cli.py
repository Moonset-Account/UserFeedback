"""CLI 主入口 app.py 的单元测试(用子进程真实调用,仅本地)。"""

import json
import os
import subprocess
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(REPO_ROOT, "app.py")
SAMPLE_CONFIG = os.path.join(REPO_ROOT, "samples", "config.json")


def run_cli(*argv):
    return subprocess.run(
        [sys.executable, APP] + list(argv),
        capture_output=True, text=True, timeout=30)


class TestCli(unittest.TestCase):
    def test_generate_outputs_rules_for_all_branches(self):
        proc = run_cli("generate", "--config", SAMPLE_CONFIG)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual(set(out), {"bj-01", "sh-02"})
        bj = {r["suffix"]: r for r in out["bj-01"]}
        self.assertEqual(bj["corp.example.com"]["resolver"], "internal-bj")

    def test_query_branch_override(self):
        proc = run_cli("query", "--config", SAMPLE_CONFIG,
                       "--branch", "bj-01", "--name", "www.corp.example.com")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["resolver"]["id"], "internal-bj")

    def test_query_internal_failure_not_forwarded(self):
        proc = run_cli("query", "--config", SAMPLE_CONFIG,
                       "--branch", "sh-02", "--name", "db.legacy.example.com")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual(out["status"], "internal_failure")
        self.assertFalse(out["forwarded_to_public"])

    def test_query_unknown_branch_exit_3(self):
        proc = run_cli("query", "--config", SAMPLE_CONFIG,
                       "--branch", "gz-99", "--name", "a.corp.example.com")
        self.assertEqual(proc.returncode, 3)
        self.assertIn("unknown branch", proc.stderr)

    def test_missing_config_file_exit_2(self):
        proc = run_cli("generate", "--config",
                       os.path.join(REPO_ROOT, "samples", "no-such.json"))
        self.assertEqual(proc.returncode, 2)


if __name__ == "__main__":
    unittest.main()
