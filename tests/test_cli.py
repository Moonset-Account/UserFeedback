"""CLI entry point: exit codes, output formats and file handling."""

import io
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout

import app

from .helpers import export, fixture_path, record, write_json


class CliTests(unittest.TestCase):
    def run_cli(self, argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = app.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_markdown_summary_from_delivered_fixtures(self):
        code, out, err = self.run_cli([
            "--baseline", fixture_path("baseline.json"),
            "--current", fixture_path("current.json"),
        ])
        self.assertEqual(code, 0)
        self.assertIn("# Parser regression summary", out)
        self.assertIn("## New failures (4)", out)
        self.assertIn("## Persistent failures (4)", out)
        self.assertIn("## Unstable (1)", out)
        self.assertIn("## Not executed (1)", out)
        self.assertIn("## Needs confirmation", out)
        for name in ("flaky-net", "brand-new", "latency-probe", "skip-to-fail"):
            self.assertIn(name, out)
        # Attempts, build and environment must be visible per row.
        self.assertIn("build-2026-11-14", out)
        self.assertIn("| strict |", out)
        self.assertIn("new_timeout", out)
        self.assertIn("pre_existing_timeout", out)
        self.assertIn("source: current-build-export", out)
        self.assertIn("| Content summary |", out)
        self.assertIn("new_failures=4", err)
        # A pending-confirmation row must not be annotated as a new category.
        conflict_line = next(line for line in out.splitlines()
                             if line.startswith("| dup-sample"))
        self.assertNotIn("new failure category", conflict_line)

    def test_json_format_is_machine_readable(self):
        import json

        code, out, _ = self.run_cli([
            "--baseline", fixture_path("baseline.json"),
            "--current", fixture_path("current.json"),
            "--format", "json",
        ])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["counts"]["new_failures"], 4)
        self.assertEqual(payload["baseline"]["build_id"], "base-2026-11-01")

    def test_output_file(self):
        path = os.path.join(os.path.dirname(fixture_path("baseline.json")),
                            "_cli_out.md")
        try:
            code, out, _ = self.run_cli([
                "--baseline", fixture_path("baseline.json"),
                "--current", fixture_path("current.json"),
                "--output", path,
            ])
            self.assertEqual(code, 0)
            self.assertEqual(out, "")
            with open(path, "r", encoding="utf-8") as handle:
                self.assertIn("## New failures (4)", handle.read())
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def test_exit_one_only_when_requested_and_new_failures_exist(self):
        code, _, _ = self.run_cli([
            "--baseline", fixture_path("baseline.json"),
            "--current", fixture_path("current.json"),
            "--exit-on-new-failure",
        ])
        self.assertEqual(code, 1)

    def test_exit_zero_when_no_new_failures(self):
        baseline = write_json(export("b1", [record("s", content="c", status="passed")]))
        current = write_json(export("b2", [record("s", content="c", status="passed")]))
        try:
            code, out, _ = self.run_cli([
                "--baseline", baseline, "--current", current,
                "--exit-on-new-failure",
            ])
        finally:
            os.unlink(baseline)
            os.unlink(current)
        self.assertEqual(code, 0)
        self.assertIn("## New failures (0)", out)

    def test_invalid_input_exits_two_with_message(self):
        bad = write_json({"build_id": "b1", "results": []})
        try:
            code, out, err = self.run_cli([
                "--baseline", bad, "--current", fixture_path("current.json"),
            ])
        finally:
            os.unlink(bad)
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("error: baseline", err)

    def test_missing_file_exits_two(self):
        code, _, err = self.run_cli([
            "--baseline", os.path.join(os.path.dirname(__file__), "nope.json"),
            "--current", fixture_path("current.json"),
        ])
        self.assertEqual(code, 2)
        self.assertIn("cannot read file", err)


if __name__ == "__main__":
    unittest.main()
