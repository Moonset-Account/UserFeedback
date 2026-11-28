"""Tests for the scripted demonstrations and the CLI."""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr, redirect_stdout

from fpiso import demo
from fpiso.cli import main


class DemoScenarioTests(unittest.TestCase):
    def test_all_scenarios_run_and_are_nonempty(self) -> None:
        for name, function in demo.all_scenarios():
            lines = function()
            self.assertIsInstance(lines, list, name)
            self.assertTrue(lines, name)
            self.assertTrue(any(line.startswith("===") for line in lines), name)

    def test_midpoint_scenario_values(self) -> None:
        text = "\n".join(demo.scenario_midpoints())
        self.assertIn("rne: value 1", text)
        self.assertIn("rna: value 9/8", text)
        self.assertIn("rz: value 1", text)
        self.assertIn("rd: value 1", text)
        self.assertIn("ru: value 9/8", text)
        self.assertIn("{inexact}", text)

    def test_mask_scenario_shows_pending_difference(self) -> None:
        text = "\n".join(demo.scenario_masks())
        self.assertIn("all-masked", text)
        self.assertIn("all-unmasked", text)
        self.assertIn("generated=[]", text)
        self.assertIn("generated=[inexact]", text)

    def test_nested_scenario_shows_isolation(self) -> None:
        text = "\n".join(demo.scenario_nested())
        self.assertIn("SuspendedDomainError", text)
        self.assertIn("the audit's underflow never entered the outer domain", text)
        self.assertIn("generated=[underflow]", text)

    def test_fma_scenario_shows_single_rounding(self) -> None:
        text = "\n".join(demo.scenario_fma())
        self.assertIn("81/64", text)
        self.assertIn("1/64", text)
        # The fused path flags underflow, the split path does not.
        self.assertIn("underflow, inexact", text)

    def test_contrast_scenario_illustrates_leak(self) -> None:
        text = "\n".join(demo.scenario_contrast())
        self.assertIn("inner underflow survives", text)
        self.assertIn("{underflow, inexact}", text)

    def test_scenarios_are_deterministic(self) -> None:
        for name, function in demo.all_scenarios():
            self.assertEqual(function(), function(), name)


class CliTests(unittest.TestCase):
    def run_cli(self, argv):
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_demo_all(self) -> None:
        code, out, _ = self.run_cli(["demo"])
        self.assertEqual(code, 0)
        self.assertIn("scenario 1", out)
        self.assertIn("scenario 6", out)

    def test_demo_single(self) -> None:
        code, out, _ = self.run_cli(["demo", "fma"])
        self.assertEqual(code, 0)
        self.assertIn("multiply-add", out)
        self.assertNotIn("scenario 1", out)

    def test_demo_unknown(self) -> None:
        with self.assertRaises(SystemExit):
            self.run_cli(["demo", "nope"])

    def test_op_add(self) -> None:
        code, out, _ = self.run_cli(["op", "add", "1", "1/16", "--rounding", "rna"])
        self.assertEqual(code, 0)
        self.assertIn("value  : 9/8", out)
        self.assertIn("flags  : {inexact}", out)

    def test_op_masks_unmasked(self) -> None:
        code, out, _ = self.run_cli(
            ["op", "mul", "1/3", "1", "--masks", "inexact", "underflow"]
        )
        self.assertEqual(code, 0)
        self.assertIn("pending: [inexact]", out)

    def test_op_fma_trace(self) -> None:
        code, out, _ = self.run_cli(["op", "fma", "9/8", "9/8", "-5/4"])
        self.assertEqual(code, 0)
        self.assertIn("exact product: 81/64", out)
        self.assertIn("exact sum    : 1/64", out)

    def test_op_invalid_operand_reports_error(self) -> None:
        code, _, err = self.run_cli(["op", "add", "banana", "1"])
        self.assertEqual(code, 2)
        self.assertIn("error:", err)

    def test_op_negative_operands(self) -> None:
        code, out, _ = self.run_cli(["op", "add", "-1", "-1/16", "--rounding", "rd"])
        self.assertEqual(code, 0)
        self.assertIn("value  : -9/8", out)

    def test_op_binary_float_string_is_treated_as_exact_decimal(self) -> None:
        # "0.1" is an *exact* decimal string in the model, not a binary float.
        code, out, _ = self.run_cli(["op", "mul", "0.1", "10"])
        self.assertEqual(code, 0)
        self.assertIn("value  : 1", out)

    def test_op_format_flags(self) -> None:
        code, out, _ = self.run_cli(
            [
                "op",
                "add",
                "1",
                "1",
                "--format",
                "mini",
                "--precision",
                "3",
                "--emin",
                "-1",
                "--emax",
                "1",
            ]
        )
        self.assertEqual(code, 0)
        self.assertIn("mini", out)

    def test_formats_listing(self) -> None:
        code, out, _ = self.run_cli(["formats"])
        self.assertEqual(code, 0)
        self.assertIn("small(", out)
        self.assertIn("rounding attributes", out)

    def test_no_arguments_runs_demo(self) -> None:
        code, out, _ = self.run_cli([])
        self.assertEqual(code, 0)
        self.assertIn("scenario 1", out)


if __name__ == "__main__":
    unittest.main()
