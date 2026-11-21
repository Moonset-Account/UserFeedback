import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run_cli(*args, cwd=ROOT):
    return subprocess.run(
        [sys.executable, "-m", "flowupdate", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
    )


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.problem = os.path.join(self.tmp.name, "p.json")
        with open(self.problem, "w", encoding="utf-8") as handle:
            json.dump(
                {"old": {"A": "B", "B": "OUT"}, "new": {"A": "OUT", "B": "A"}},
                handle,
            )

    def tearDown(self):
        self.tmp.cleanup()

    def test_solve_prints_two_rounds(self):
        result = run_cli("solve", self.problem)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["round_count"], 2)
        self.assertEqual(payload["rounds"], [["A"], ["B"]])

    def test_solve_max_round_size_one(self):
        result = run_cli("solve", self.problem, "--max-round-size", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["round_count"], 2)

    def test_verify_good_plan(self):
        plan = os.path.join(self.tmp.name, "plan.json")
        with open(plan, "w", encoding="utf-8") as handle:
            json.dump([["A"], ["B"]], handle)
        result = run_cli("verify", self.problem, plan)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("OK", result.stdout)

    def test_verify_bad_plan_exit_code(self):
        plan = os.path.join(self.tmp.name, "bad.json")
        with open(plan, "w", encoding="utf-8") as handle:
            json.dump([["B"], ["A"]], handle)
        result = run_cli("verify", self.problem, plan)
        self.assertEqual(result.returncode, 1)
        self.assertIn("FAILED", result.stdout)

    def test_check_unsafe_round(self):
        result = run_cli("check", self.problem, "A", "B")
        self.assertEqual(result.returncode, 1)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["safe"])
        self.assertEqual(payload["failing_subset"], ["B"])

    def test_check_safe_round(self):
        result = run_cli("check", self.problem, "A")
        self.assertEqual(result.returncode, 0)
        self.assertTrue(json.loads(result.stdout)["safe"])

    def test_invalid_problem_exit_code_two(self):
        bad = os.path.join(self.tmp.name, "bad.json")
        with open(bad, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        result = run_cli("solve", bad)
        self.assertEqual(result.returncode, 2)
        self.assertIn("error:", result.stderr)


if __name__ == "__main__":
    unittest.main()
