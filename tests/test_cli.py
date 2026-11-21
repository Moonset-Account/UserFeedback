"""Acceptance tests for the command-line entry point.

These exercise the real entry point (``python -m joinplan plan``) as a
subprocess so the tests fail if argument handling, exit codes, or JSON
printing regress, not just the library functions.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

WORKSPACE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run_cli(*args, stdin_text=None):
    return subprocess.run(
        [sys.executable, "-m", "joinplan", *args],
        cwd=WORKSPACE,
        input=stdin_text,
        capture_output=True,
        text=True,
    )


REQUEST = {
    "nodes": 4,
    "tables": [
        {"name": "A", "bytes": 2000, "partition_key": "k"},
        {"name": "B", "bytes": 1000},
    ],
    "edges": [{"left": "A.k", "right": "B.k"}],
    "outputs": [{"tables": ["A", "B"], "bytes": 5000}],
}


class CliTests(unittest.TestCase):
    def test_plans_from_a_file_and_prints_step_costs(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "query.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(REQUEST, handle)

            result = run_cli("plan", path)

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["total_cost"], 1000)
        self.assertEqual(payload["result_tables"], ["A", "B"])
        self.assertEqual(len(payload["steps"]), 1)
        step = payload["steps"][0]
        self.assertEqual(step["method"], "repartition")
        self.assertEqual(step["cumulative_cost"], 1000)
        self.assertEqual(step["cost"], 1000)

    def test_reads_standard_input_with_dash(self):
        result = run_cli("plan", "-", stdin_text=json.dumps(REQUEST))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["total_cost"], 1000)

    def test_invalid_input_exits_2_with_diagnostic(self):
        result = run_cli("plan", "-", stdin_text=json.dumps({"nodes": 0}))
        self.assertEqual(result.returncode, 2)
        self.assertIn("input error", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_malformed_json_exits_2(self):
        result = run_cli("plan", "-", stdin_text="{oops")
        self.assertEqual(result.returncode, 2)
        self.assertIn("invalid JSON", result.stderr)

    def test_missing_file_exits_2(self):
        result = run_cli("plan", "/nonexistent/query.json")
        self.assertEqual(result.returncode, 2)
        self.assertIn("cannot read input file", result.stderr)

    def test_missing_subcommand_exits_2(self):
        result = run_cli()
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
