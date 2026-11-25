import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class CliTests(unittest.TestCase):
    def test_basic_json_file(self):
        proc = subprocess.run(
            [sys.executable, "-m", "key_ceremony", "examples/basic.json"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=20,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["feasible"])
        self.assertEqual(payload["minimum_changes"], 0)
        self.assertEqual(payload["rounds"][0]["enabled_keys"], ["k1"])

    def test_unreachable_file_uses_exit_code_two(self):
        proc = subprocess.run(
            [sys.executable, "-m", "key_ceremony", "examples/unreachable.json"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=20,
        )
        self.assertEqual(proc.returncode, 2, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertFalse(payload["feasible"])
        self.assertEqual(payload["first_unreachable_round"]["index"], 1)
        self.assertTrue(payload["first_unreachable_round"]["locally_assignable"])

    def test_stdin_and_json_api(self):
        spec = json.dumps(
            {
                "people": [{"id": "A"}, {"id": "B"}],
                "rounds": [[{"slot": "s1"}, "s2"]],
            }
        )
        proc = subprocess.run(
            [sys.executable, "-m", "key_ceremony"],
            input=spec,
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=20,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["feasible"])
        self.assertEqual(payload["minimum_changes"], 0)

    def test_top_level_solution_wrapper(self):
        sys.path.insert(0, str(ROOT))
        try:
            import solution

            payload = solution.solve(
                {
                    "people": [{"id": "A"}, {"id": "B"}],
                    "rounds": [["s1", "s2"]],
                }
            )
        finally:
            sys.path.remove(str(ROOT))
        self.assertTrue(payload["feasible"])
        self.assertEqual(payload["minimum_changes"], 0)


if __name__ == "__main__":
    unittest.main()
