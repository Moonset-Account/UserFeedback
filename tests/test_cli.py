"""Tests for the JSONL command-line runner."""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from median_window.__main__ import main
from median_window import MedianWindow


class CliTests(unittest.TestCase):
    def _run(self, payload: str, *args):
        with tempfile.NamedTemporaryFile(
            "w", suffix=".jsonl", delete=False, encoding="utf-8"
        ) as fh:
            fh.write(payload)
            path = fh.name
        try:
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = main(["--window", "2", path, *args])
            return code, out.getvalue(), err.getvalue()
        finally:
            os.unlink(path)

    def test_object_records(self):
        payload = "\n".join(
            [
                json.dumps({"partition": "a", "time": 0, "value": 10}),
                json.dumps({"partition": "a", "time": 1, "value": 2}),
                json.dumps({"partition": "a", "time": 1, "value": 4}),
                json.dumps({"partition": "a", "time": 2, "value": 7}),
            ]
        )
        code, out, _ = self._run(payload)
        self.assertEqual(code, 0)
        rows = [json.loads(line) for line in out.splitlines()]
        self.assertEqual([r["row"] for r in rows], [1, 2, 3, 4])
        # t=2 sees [0,2) = {10,2,4} -> 4
        self.assertEqual([r["median"] for r in rows], [None, 10, 10, 4])

    def test_array_records_match_api(self):
        records = [["a", 0, 3], ["a", 1, 9], ["b", 0, 5], ["b", 2, 1]]
        payload = "\n".join(json.dumps(r) for r in records)
        code, out, _ = self._run(payload)
        self.assertEqual(code, 0)
        cli_medians = [json.loads(line)["median"] for line in out.splitlines()]
        op = MedianWindow(2, retain_results=True)
        op.push_many([tuple(r) for r in records])
        self.assertEqual(cli_medians, [r.median for r in op.results])

    def test_stats_go_to_stderr(self):
        payload = json.dumps({"partition": "a", "time": 0, "value": 1})
        code, _, err = self._run(payload, "--stats")
        self.assertEqual(code, 0)
        stats = json.loads(err)
        self.assertEqual(stats["rows_in"], 1)
        self.assertEqual(stats["rebuilds"], 0)

    def test_bad_json_reports_error(self):
        code, out, err = self._run("{not json}\n")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("error:", err)

    def test_ordering_violation_reports_error(self):
        payload = "\n".join(
            [
                json.dumps({"partition": "a", "time": 5, "value": 1}),
                json.dumps({"partition": "a", "time": 4, "value": 2}),
            ]
        )
        code, _, err = self._run(payload)
        self.assertEqual(code, 2)
        self.assertIn("error:", err)

    def test_null_value_round_trips(self):
        payload = "\n".join(
            [
                json.dumps({"partition": "a", "time": 0, "value": None}),
                json.dumps({"partition": "a", "time": 1, "value": 4}),
            ]
        )
        code, out, _ = self._run(payload)
        self.assertEqual(code, 0)
        rows = [json.loads(line) for line in out.splitlines()]
        self.assertEqual([r["median"] for r in rows], [None, None])


if __name__ == "__main__":
    unittest.main()
