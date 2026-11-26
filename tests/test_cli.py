import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from static_check.__main__ import main


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "job_error_flow.json"


class CliTests(unittest.TestCase):
    def test_passing_policy_returns_zero_and_json(self):
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(
                [
                    str(EXAMPLE),
                    "--caller",
                    "agent-1",
                    "--public",
                    "diag.input.path,diag.job.id,diag.recovery.id",
                    "--codes",
                    "E_IO,E_JOB",
                    "--json",
                ]
            )
        self.assertEqual(code, 0)
        report = json.loads(output.getvalue())
        self.assertTrue(report["ok"])
        self.assertEqual(len(report["exits"]), 2)

    def test_restrictive_policy_returns_one(self):
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(
                [
                    str(EXAMPLE),
                    "--caller",
                    "agent-1",
                    "--public",
                    "diag.job.id",
                    "--codes",
                    "E_JOB",
                ]
            )
        self.assertEqual(code, 1)
        self.assertIn("UNDECLARED_DIAGNOSTIC", output.getvalue())


if __name__ == "__main__":
    unittest.main()
