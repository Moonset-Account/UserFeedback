"""Boundary, error-handling and CLI tests."""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest

from relanalysis import (
    MAX_COLUMNS,
    DataError,
    DependencyError,
    SchemaError,
    analyze,
    analyze_problem,
    verify_evidence,
)
from relanalysis.cli import main

WORKSPACE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class BoundaryTests(unittest.TestCase):
    def test_five_columns_supported(self):
        result = analyze(
            ["A", "B", "C", "D", "E"],
            [("R1", ["A", "B"]), ("R2", ["B", "C"]), ("R3", ["C", "D"]), ("R4", ["D", "E"])],
            [("FD", ("D",), ("E",))],
        )
        self.assertEqual(len(result.schema.columns), MAX_COLUMNS)

    def test_six_columns_rejected(self):
        with self.assertRaises(SchemaError):
            analyze(
                list("ABCDEF"),
                [("R1", ["A", "B", "C"]), ("R2", ["D", "E", "F"])],
                [],
            )

    def test_one_column_schema(self):
        result = analyze(["A"], [("R1", ["A"])], [])
        self.assertTrue(result.lossless)

    def test_one_column_mvd_is_trivial_and_lossy(self):
        # A ->> B over just {A,B}: splitting into R1(A), R2(B) loses the pairing
        result = analyze(["A", "B"], [("R1", ["A"]), ("R2", ["B"])], [("MVD", ("A",), ("B",))])
        self.assertFalse(result.lossless)

    def test_subtable_repeating_a_column_rejected(self):
        with self.assertRaises(SchemaError):
            analyze(["A", "B"], [("R1", ["A", "A"]), ("R2", ["B"])], [])

    def test_duplicate_subtable_layouts_allowed(self):
        result = analyze(["A", "B"], [("R1", ["A", "B"]), ("R2", ["A", "B"])], [])
        self.assertTrue(result.lossless)

    def test_degenerate_values(self):
        # only equality is interpreted, so any column name shape is fine
        result = analyze(["K", "V"], [("R1", ["K", "V"])], [("FD", ("K",), ("V",))])
        self.assertTrue(result.lossless)
        self.assertTrue(verify_evidence(result.to_dict()).accepted)

    def test_columns_that_look_like_symbols_do_not_collide(self):
        # column names may coincide with the internal symbol syntax
        result = analyze(
            ["a:A", "B"],
            [("R1", ["a:A"]), ("R2", ["B"])],
            [("MVD", ("a:A",), ("B",))],
        )
        self.assertFalse(result.lossless)
        self.assertTrue(verify_evidence(result.to_dict()).accepted)

    def test_mvd_empty_left_side_allowed(self):
        # {} ->> A forces A to be independent of B, so R1(A) join R2(B) is exact
        result = analyze(["A", "B"], [("R1", ["A"]), ("R2", ["B"])], [("MVD", (), ("A",))])
        self.assertTrue(result.lossless)
        self.assertTrue(verify_evidence(result.to_dict()).accepted)


class InputErrorTests(unittest.TestCase):
    def test_unknown_subtable_column(self):
        with self.assertRaises(SchemaError):
            analyze(["A", "B"], [("R1", ["A", "Z"]), ("R2", ["B"])], [])

    def test_missing_coverage(self):
        with self.assertRaises(SchemaError):
            analyze(["A", "B", "C"], [("R1", ["A", "B"])], [])

    def test_bad_dependency_tuple(self):
        with self.assertRaises(DependencyError):
            analyze(["A", "B"], [("R1", ["A", "B"])], [("FD", "A")])

    def test_mvd_overlap(self):
        with self.assertRaises(DependencyError):
            analyze(["A", "B"], [("R1", ["A", "B"])], [("MVD", ("A",), ("A",))])

    def test_instance_null_rejected(self):
        from relanalysis.model import Instance, Schema

        with self.assertRaises(DataError):
            Instance.from_rows(Schema.from_columns(["A"]), [{"A": None}])


class ProblemDocumentTests(unittest.TestCase):
    PROBLEM = {
        "schema": ["A", "B", "C"],
        "subtables": [
            {"name": "R1", "columns": ["A", "B"]},
            {"name": "R2", "columns": ["B", "C"]},
        ],
        "dependencies": [{"kind": "FD", "left": ["B"], "right": ["C"]}],
    }

    def test_analyze_problem(self):
        result = analyze_problem(self.PROBLEM)
        self.assertTrue(result.lossless)

    def test_missing_key(self):
        with self.assertRaises(KeyError):
            analyze_problem({"schema": ["A"]})


class CliTests(unittest.TestCase):
    def _write(self, directory, name, payload):
        path = os.path.join(directory, name)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        return path

    def test_cli_analyse_prints_and_self_verifies(self):
        with tempfile.TemporaryDirectory() as tmp:
            problem = self._write(tmp, "problem.json", ProblemDocumentTests.PROBLEM)
            output = io.StringIO()
            old = sys.stdout
            sys.stdout = output
            try:
                code = main([problem])
            finally:
                sys.stdout = old
        self.assertEqual(code, 0)
        self.assertIn("verdict: lossless", output.getvalue())
        self.assertIn("ACCEPTED", output.getvalue())

    def test_cli_json_output_verifies(self):
        with tempfile.TemporaryDirectory() as tmp:
            problem = self._write(tmp, "problem.json", ProblemDocumentTests.PROBLEM)
            output = io.StringIO()
            old = sys.stdout
            sys.stdout = output
            try:
                code = main([problem, "--json"])
            finally:
                sys.stdout = old
        self.assertEqual(code, 0)
        document = json.loads(output.getvalue())
        self.assertTrue(verify_evidence(document).accepted)
        self.assertEqual(document["verdict"], "lossless")

    def test_cli_verify_command_accepts(self):
        with tempfile.TemporaryDirectory() as tmp:
            problem = self._write(tmp, "problem.json", ProblemDocumentTests.PROBLEM)
            result = analyze_problem(ProblemDocumentTests.PROBLEM)
            evidence = self._write(tmp, "evidence.json", result.to_dict())
            output = io.StringIO()
            old = sys.stdout
            sys.stdout = output
            try:
                code = main(["--verify", evidence])
            finally:
                sys.stdout = old
        self.assertEqual(code, 0)
        self.assertIn("ACCEPTED", output.getvalue())

    def test_cli_verify_command_rejects_tampered(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = analyze_problem(ProblemDocumentTests.PROBLEM)
            document = result.to_dict()
            document["lossless"]["witness_row_id"] = 99
            evidence = self._write(tmp, "evidence.json", document)
            output = io.StringIO()
            old = sys.stdout
            sys.stdout = output
            try:
                code = main(["--verify", evidence])
            finally:
                sys.stdout = old
        self.assertEqual(code, 1)
        self.assertIn("REJECTED", output.getvalue())

    def test_module_entrypoint_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            problem = self._write(tmp, "problem.json", ProblemDocumentTests.PROBLEM)
            # cwd is the workspace so ``-m relanalysis`` resolves the package;
            # no -I here because isolated mode removes cwd from sys.path.
            completed = subprocess.run(
                [sys.executable, "-m", "relanalysis", problem],
                cwd=WORKSPACE,
                capture_output=True,
                text=True,
                env={
                    "PATH": os.environ.get("PATH", ""),
                    "HOME": os.environ.get("HOME", ""),
                },
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("verdict: lossless", completed.stdout)


if __name__ == "__main__":
    unittest.main()
