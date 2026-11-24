"""Tests for the required two-scenario probe trade-off and objective."""

from __future__ import annotations

import copy
import unittest

from adaptive_planner import (
    audit_decision_traces,
    parse_problem,
    plan_strategy,
    verify_strategy,
)
from adaptive_planner.example_data import probe_example


class ProbeTradeOffTests(unittest.TestCase):
    def setUp(self) -> None:
        self.data = probe_example()
        self.problem = parse_problem(copy.deepcopy(self.data))
        self.strategy = plan_strategy(self.problem)

    def test_adaptive_objective_and_paths(self) -> None:
        obj = self.strategy["objective"]
        self.assertEqual(obj["worst_total_cost"], 161)
        self.assertEqual(obj["sum_of_scenario_total_costs"], 268)
        self.assertEqual(
            obj["scenario_total_costs"],
            {"small-A": 107, "small-C": 161},
        )

        paths = self.strategy["paths"]
        small_a = paths["small-A"]
        small_c = paths["small-C"]

        self.assertEqual(
            [(s["kind"], s.get("algorithm") or s["action"].get("algorithm"), s.get("edge_id") or s["action"].get("edge_id"))
             for s in small_a[:-1]],
            [
                ("materialize", None, None),
                ("join", "hash", "AB"),
                ("join", "hash", "BC"),
            ],
        )
        self.assertEqual(
            [(s["kind"], s.get("algorithm") or s["action"].get("algorithm"), s.get("edge_id") or s["action"].get("edge_id"))
             for s in small_c[:-1]],
            [
                ("materialize", None, None),
                ("join", "hash", "BC"),
                ("join", "hash", "AB"),
            ],
        )

        # Exact costs from the integer affine fee table.
        self.assertEqual([s["cost"] for s in small_a[:-1]], [2, 3, 102])
        self.assertEqual([s["cost"] for s in small_c[:-1]], [20, 21, 120])
        self.assertEqual(small_a[-1]["cumulative_cost"], 107)
        self.assertEqual(small_c[-1]["cumulative_cost"], 161)

    def test_common_probe_is_first_decision(self) -> None:
        root = self.strategy["nodes"][self.strategy["root_node"]]
        first_id = root["branches"][0]["next_node"]
        first = self.strategy["nodes"][first_id]
        self.assertEqual(first["kind"], "decision")
        self.assertEqual(first["action"]["kind"], "materialize")
        self.assertEqual(first["action"]["subset"], "B")

        observations = sorted(
            branch["observed_rows"] for branch in first["branches"]
        )
        self.assertEqual(observations, [2, 20])

    def test_probe_reduces_worst_vs_best_fixed_plan(self) -> None:
        fixed = plan_strategy(self.problem, allow_materialization=False)
        self.assertEqual(fixed["objective"]["worst_total_cost"], 303)
        self.assertEqual(fixed["objective"]["sum_of_scenario_total_costs"], 444)

        first_fixed = fixed["nodes"][fixed["root_node"]]["branches"][0]["next_node"]
        action = fixed["nodes"][first_fixed]["action"]
        self.assertEqual((action["algorithm"], action["edge_id"]), ("hash", "BC"))

        adaptive_worst = self.strategy["objective"]["worst_total_cost"]
        self.assertLess(adaptive_worst, fixed["objective"]["worst_total_cost"])

    def test_each_scenario_prefers_a_different_first_join(self) -> None:
        first_edges = {}
        for scenario in self.data["scenarios"]:
            one_world = copy.deepcopy(self.data)
            one_world["scenarios"] = [copy.deepcopy(scenario)]
            problem = parse_problem(one_world)
            result = plan_strategy(problem)
            root = result["nodes"][result["root_node"]]
            node = result["nodes"][root["branches"][0]["next_node"]]
            self.assertEqual(node["action"]["kind"], "join")
            first_edges[scenario["id"]] = node["action"]["edge_id"]

        self.assertEqual(
            first_edges,
            {"small-A": "AB", "small-C": "BC"},
        )

    def test_generated_strategy_passes_offline_optimal_verification(self) -> None:
        report = verify_strategy(self.problem, self.strategy, check_optimal=True)
        self.assertTrue(report.ok, msg="; ".join(report.errors))
        self.assertEqual(report.leak_violations, [])
        self.assertEqual(report.checked_scenarios, 2)

    def test_switch_positions_identify_divergence_after_step_one(self) -> None:
        for sid, rows in (("small-A", 2), ("small-C", 20)):
            switches = self.strategy["switches"][sid]
            self.assertEqual(len(switches), 1)
            switch = switches[0]
            self.assertEqual(switch["after_step"], 1)
            self.assertEqual(switch["subset"], "B")
            self.assertEqual(switch["observed_rows"], rows)
            self.assertTrue(switch["causes_switch"])
            next_actions = {b["next_action"] for b in switch["branches"]}
            self.assertTrue(any("hash_join(A, B)" in x for x in next_actions))
            self.assertTrue(any("hash_join(B, C)" in x for x in next_actions))


class TraceAuditTests(unittest.TestCase):
    def test_consistent_prefixes_pass(self) -> None:
        traces = {
            "s1": [
                {"action": "probe B", "subset": "B", "observed_rows": 2},
                {"action": "AB first"},
            ],
            "s2": [
                {"action": "probe B", "subset": "B", "observed_rows": 2},
                {"action": "AB first"},
            ],
            "s3": [
                {"action": "probe B", "subset": "B", "observed_rows": 20},
                {"action": "BC first"},
            ],
        }
        report = audit_decision_traces(traces)
        self.assertTrue(report.ok, report.errors)

    def test_different_first_action_without_observation_is_leak(self) -> None:
        traces = {
            "s1": [{"action": "AB first"}],
            "s2": [{"action": "BC first"}],
        }
        report = audit_decision_traces(traces)
        self.assertFalse(report.ok)
        self.assertTrue(report.leak_violations)
        self.assertIn("different actions", report.errors[0])

    def test_same_observation_must_have_same_next_action(self) -> None:
        traces = {
            "s1": [
                {"action": "probe B", "subset": "B", "observed_rows": 7},
                {"action": "AB"},
            ],
            "s2": [
                {"action": "probe B", "subset": "B", "observed_rows": 7},
                {"action": "BC"},
            ],
        }
        report = audit_decision_traces(traces)
        self.assertFalse(report.ok)
        self.assertEqual(len(report.leak_violations), 1)


if __name__ == "__main__":
    unittest.main()
