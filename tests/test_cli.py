"""CLI tests: run app.py as a subprocess against the bundled samples and
against temporary fixtures, asserting exit codes and JSON output."""

import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "app.py")
SAMPLES = os.path.join(ROOT, "samples")


def run_cli(*argv):
    return subprocess.run(
        [sys.executable, APP, *argv],
        capture_output=True, text=True, timeout=30)


def sample_path(name):
    return os.path.join(SAMPLES, name)


class CliDiffTest(unittest.TestCase):
    def test_diff_json_on_sample(self):
        proc = run_cli("diff",
                       "--current", sample_path("reorder_current.json"),
                       "--candidate", sample_path("reorder_candidate.json"),
                       "--format", "json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(len(report["changes"]), 2)
        self.assertEqual(len(report["rule_only_changes"]), 1)
        self.assertEqual(report["changes"][0]["before"]["action"], "allow")
        self.assertEqual(report["changes"][0]["after"]["action"], "deny")

    def test_diff_text_mentions_rules(self):
        proc = run_cli("diff",
                       "--current", sample_path("reorder_current.json"),
                       "--candidate", sample_path("reorder_candidate.json"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("deny-office-to-db", proc.stdout)
        self.assertIn("allow-all-internal", proc.stdout)
        self.assertIn("10.30.8.0/24", proc.stdout)


class CliCheckTest(unittest.TestCase):
    def test_check_exit_1_when_unsatisfied(self):
        proc = run_cli("check",
                       "--candidate", sample_path("reorder_candidate.json"),
                       "--business", sample_path("reorder_business.json"),
                       "--format", "json")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        report = json.loads(proc.stdout)
        businesses = {u["business"] for u in report["unsatisfied"]}
        self.assertEqual(businesses, {"biz-office-to-db", "biz-partner-to-db"})

    def test_check_exit_0_when_all_satisfied(self):
        business = [{
            "id": "ok", "src": "10.20.7.0/24", "dst": "10.30.8.0/24",
            "proto": "tcp", "port": "5432", "expect": "allow",
        }]
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "biz.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(business, fh)
            proc = run_cli("check",
                           "--candidate", sample_path("reorder_candidate.json"),
                           "--business", path)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("(none)", proc.stdout)


class CliReviewTest(unittest.TestCase):
    def test_review_combines_diff_and_check(self):
        proc = run_cli("review",
                       "--current", sample_path("reorder_current.json"),
                       "--candidate", sample_path("reorder_candidate.json"),
                       "--business", sample_path("reorder_business.json"),
                       "--format", "json")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertIn("changes", report)
        self.assertIn("rule_only_changes", report)
        self.assertIn("unsatisfied", report)
        self.assertEqual(len(report["changes"]), 2)
        self.assertEqual(len(report["unsatisfied"]), 2)

    def test_sample_command_runs(self):
        proc = run_cli("sample")
        self.assertIn(proc.returncode, (0, 1), proc.stderr)
        self.assertIn("allow-ops-to-db", proc.stdout)


class CliErrorTest(unittest.TestCase):
    def test_missing_file_exit_2(self):
        proc = run_cli("diff", "--current", "/nonexistent/a.json",
                       "--candidate", "/nonexistent/b.json")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("error:", proc.stderr)

    def test_invalid_rule_exit_2(self):
        bad_rules = [{"id": "x", "action": "allow", "port": "70000"}]
        with tempfile.TemporaryDirectory() as tmp:
            bad = os.path.join(tmp, "bad.json")
            with open(bad, "w", encoding="utf-8") as fh:
                json.dump(bad_rules, fh)
            proc = run_cli("diff", "--current", bad,
                           "--candidate", sample_path("reorder_candidate.json"))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("port", proc.stderr)

    def test_malformed_json_exit_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = os.path.join(tmp, "bad.json")
            with open(bad, "w", encoding="utf-8") as fh:
                fh.write("{not json")
            proc = run_cli("diff", "--current", bad,
                           "--candidate", sample_path("reorder_candidate.json"))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("invalid JSON", proc.stderr)


if __name__ == "__main__":
    unittest.main()
