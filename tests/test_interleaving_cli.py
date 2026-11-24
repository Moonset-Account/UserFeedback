"""Interleaving/equivalence tests and end-to-end command-line checks."""

from __future__ import annotations

import copy
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from adaptive_planner.cli import main as cli_main
from adaptive_planner.example_data import probe_example
from adaptive_planner.model import parse_problem
from adaptive_planner.planner import plan_strategy
from adaptive_planner.validator import verify_strategy


class ObservationInterleavingTests(unittest.TestCase):
    def test_same_observation_groups_scenarios_and_forces_one_action(self) -> None:
        data = copy.deepcopy(probe_example())
        twin = copy.deepcopy(data["scenarios"][0])
        twin["id"] = "small-A-late-divergence"
        # Same pre-decision counts and same B observation, but a different late
        # output cardinality that is not observed by an additional probe.
        twin["rows"]["A+B+C"] = 9999
        data["scenarios"].append(twin)

        problem = parse_problem(data)
        strategy = plan_strategy(problem)
        report = verify_strategy(problem, strategy, check_optimal=True)
        self.assertTrue(report.ok, report.errors)

        first_id = strategy["nodes"][strategy["root_node"]]["branches"][0]["next_node"]
        first = strategy["nodes"][first_id]
        self.assertEqual(first["action"]["subset"], "B")

        obs_two_branch = next(
            b for b in first["branches"] if b["observed_rows"] == 2
        )
        self.assertEqual(
            set(obs_two_branch["scenarios"]),
            {"small-A", "small-A-late-divergence"},
        )
        child = strategy["nodes"][obs_two_branch["next_node"]]
        self.assertEqual(
            set(child["scenarios"]),
            {"small-A", "small-A-late-divergence"},
        )
        self.assertEqual(child["action"]["edge_id"], "AB")

        # Both scenarios execute the same recorded action sequence.
        p1 = strategy["paths"]["small-A"]
        p2 = strategy["paths"]["small-A-late-divergence"]
        self.assertEqual(
            [s["node_id"] for s in p1 if s["kind"] != "terminal"],
            [s["node_id"] for s in p2 if s["kind"] != "terminal"],
        )


class CliEndToEndTests(unittest.TestCase):
    def test_solve_verify_and_audit_commands(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            problem_path = root / "problem.json"
            strategy_path = root / "strategy.json"
            traces_path = root / "traces.json"
            problem_path.write_text(json.dumps(probe_example()), encoding="utf-8")

            buf = io.StringIO()
            with redirect_stdout(buf):
                code = cli_main(
                    ["solve", str(problem_path), "-o", str(strategy_path)]
                )
            self.assertEqual(code, 0)
            self.assertTrue(strategy_path.exists())

            verify_buf = io.StringIO()
            with redirect_stdout(verify_buf):
                code = cli_main(
                    [
                        "verify",
                        str(problem_path),
                        str(strategy_path),
                        "--check-optimal",
                    ]
                )
            self.assertEqual(code, 0, verify_buf.getvalue())
            report = json.loads(verify_buf.getvalue())
            self.assertTrue(report["ok"], report)

            traces = {
                "s1": [{"action": "probe", "observed_rows": 1}, {"action": "AB"}],
                "s2": [{"action": "probe", "observed_rows": 2}, {"action": "BC"}],
            }
            traces_path.write_text(json.dumps(traces), encoding="utf-8")
            good_audit = io.StringIO()
            with redirect_stdout(good_audit):
                code = cli_main(["audit-traces", str(traces_path)])
            self.assertEqual(code, 0, good_audit.getvalue())
            self.assertTrue(json.loads(good_audit.getvalue())["ok"])

            bad_traces = {
                "s1": [{"action": "AB"}],
                "s2": [{"action": "BC"}],
            }
            traces_path.write_text(json.dumps(bad_traces), encoding="utf-8")
            bad_audit = io.StringIO()
            with redirect_stdout(bad_audit):
                code = cli_main(["audit-traces", str(traces_path)])
            self.assertEqual(code, 2)
            self.assertFalse(json.loads(bad_audit.getvalue())["ok"])

    def test_verify_rejects_tampered_objective(self) -> None:
        problem = parse_problem(probe_example())
        strategy = plan_strategy(problem)
        strategy["objective"]["worst_total_cost"] = 1
        report = verify_strategy(problem, strategy)
        self.assertFalse(report.ok)
        self.assertTrue(any("reported worst" in e for e in report.errors))

    def test_verify_rejects_tampered_switch_location(self) -> None:
        problem = parse_problem(probe_example())
        strategy = plan_strategy(problem)
        strategy["switches"]["small-A"][0]["after_step"] = 99
        report = verify_strategy(problem, strategy)
        self.assertFalse(report.ok)
        self.assertTrue(any("switch 0 field after_step" in e for e in report.errors))


class RuntimeTests(unittest.TestCase):
    def test_python_patch_version_is_the_prescribed_one(self) -> None:
        self.assertEqual(sys.version_info[:3], (3, 11, 6))


if __name__ == "__main__":
    unittest.main()
