"""Tests for the command-line front end and the shipped example."""

import json
import os
import subprocess
import sys
import unittest

WORKSPACE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLE = os.path.join(WORKSPACE, "examples", "address_avoidance.json")
PROMPT_SCENARIO = os.path.join(WORKSPACE, "examples", "prompt_scenario.json")


def _run(args, stdin=None):
    env = dict(os.environ)
    env["PYTHONPATH"] = WORKSPACE + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "wallet_planner", *args],
        cwd=WORKSPACE,
        env=env,
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )


class CliTests(unittest.TestCase):
    def test_example_file_produces_plan(self):
        result = _run([EXAMPLE])
        self.assertEqual(result.returncode, 0, result.stderr)
        decoded = json.loads(result.stdout)
        self.assertTrue(decoded["satisfied"])
        self.assertEqual(decoded["selected"][0]["group_id"], "c3.birth")
        self.assertEqual(decoded["total_weight"], "2")

    def test_prompt_scenario_example(self):
        # The canonical case: joint presentation must beat summing each
        # condition's own cheapest option.
        result = _run([PROMPT_SCENARIO])
        self.assertEqual(result.returncode, 0, result.stderr)
        decoded = json.loads(result.stdout)
        self.assertEqual(
            [item["group_id"] for item in decoded["selected"]], ["c1.g1"]
        )
        self.assertEqual(decoded["total_weight"], "2")
        by_id = {item["condition_id"]: item for item in decoded["conditions"]}
        self.assertEqual(by_id["prove-name"]["standalone_groups"], ["c2.g1"])
        self.assertFalse(by_id["prove-name"]["follows_standalone"])
        self.assertEqual(by_id["prove-name"]["extra_attributes"], ["address"])

    def test_stdin_input(self):
        with open(EXAMPLE, encoding="utf-8") as handle:
            payload = handle.read()
        result = _run(["-", "--indent", "0"], stdin=payload)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["satisfied"])

    def test_invalid_json_exits_2(self):
        result = _run(["-"], stdin="{not json")
        self.assertEqual(result.returncode, 2)
        self.assertIn("invalid JSON", result.stderr)

    def test_invalid_spec_exits_2(self):
        result = _run(["-"], stdin=json.dumps({"credentials": "nope"}))
        self.assertEqual(result.returncode, 2)
        self.assertIn("invalid specification", result.stderr)

    def test_missing_file_exits_2(self):
        result = _run([os.path.join(WORKSPACE, "no-such-file.json")])
        self.assertEqual(result.returncode, 2)

    def test_strict_unsatisfiable_exits_1(self):
        spec = {
            "credentials": [
                {
                    "id": "c1",
                    "issuer": "gov",
                    "holder": "a",
                    "groups": [{"id": "g1", "attributes": ["x"]}],
                }
            ],
            "conditions": [{"id": "k", "alternatives": [["missing"]]}],
        }
        result = _run(["--strict", "-"], stdin=json.dumps(spec))
        self.assertEqual(result.returncode, 1)
        self.assertFalse(json.loads(result.stdout)["satisfied"])


if __name__ == "__main__":
    unittest.main()
