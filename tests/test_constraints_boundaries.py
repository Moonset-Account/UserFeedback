"""Boundary, physical-property, reuse, and no-recomputation tests."""

from __future__ import annotations

import copy
import unittest

from adaptive_planner import parse_problem, plan_strategy, verify_strategy
from adaptive_planner.example_data import probe_example


def two_table_problem() -> dict:
    return {
        "tables": [
            {"name": "A", "ordering": ["A.k"]},
            {"name": "B", "ordering": ["B.k"]},
        ],
        "edges": [
            {
                "id": "AB",
                "left": "A",
                "right": "B",
                "left_keys": ["A.k"],
                "right_keys": ["B.k"],
            }
        ],
        "scenarios": [
            {"id": "s", "rows": {"A": 4, "B": 5, "A+B": 20}}
        ],
        "cost_model": {
            "join_costs": {
                "hash": {"default_cost": 100},
                "merge": {"fixed": 1},
            },
            "sort_cost": {"fixed": 3, "per_row": 2, "memory_per_row": 1},
            "materialization_cost": {"fixed": 1},
        },
        "default_memory_limit": 1000,
    }


class TieBreakTests(unittest.TestCase):
    def test_equal_worst_chooses_lower_scenario_sum(self) -> None:
        data = {
            "tables": ["A", "B"],
            "edges": [["A", "B"]],
            "scenarios": [
                {"id": "s1", "rows": {"A": 1, "B": 1, "A+B": 1}},
                {"id": "s2", "rows": {"A": 2, "B": 2, "A+B": 4}},
            ],
            "cost_model": {
                "join_costs": {
                    "hash": {
                        "default_cost": 999,
                        "rules": [
                            {"left_rows": 1, "right_rows": 1, "cost": 5},
                            {"left_rows": 2, "right_rows": 2, "cost": 10},
                        ],
                    },
                    "merge": {
                        "default_cost": 999,
                        "rules": [
                            {"left_rows": 1, "right_rows": 1, "cost": 1},
                            {"left_rows": 2, "right_rows": 2, "cost": 10},
                        ],
                    },
                },
                "materialization_cost": {"fixed": 7},
            },
        }
        problem = parse_problem(data)
        strategy = plan_strategy(problem)
        self.assertEqual(strategy["objective"]["worst_total_cost"], 10)
        self.assertEqual(strategy["objective"]["sum_of_scenario_total_costs"], 11)
        node_id = strategy["nodes"][strategy["root_node"]]["branches"][0]["next_node"]
        self.assertEqual(strategy["nodes"][node_id]["action"]["algorithm"], "merge")
        report = verify_strategy(problem, strategy, check_optimal=True)
        self.assertTrue(report.ok, report.errors)


class OrderReuseTests(unittest.TestCase):
    def test_existing_merge_order_is_reused_without_sort_fee(self) -> None:
        problem = parse_problem(two_table_problem())
        strategy = plan_strategy(problem)
        self.assertEqual(strategy["objective"]["worst_total_cost"], 1)
        node_id = strategy["nodes"][strategy["root_node"]]["branches"][0]["next_node"]
        action = strategy["nodes"][node_id]["action"]
        self.assertEqual(action["algorithm"], "merge")
        self.assertFalse(action["sort_left_required"])
        self.assertFalse(action["sort_right_required"])
        self.assertEqual(action["sort_left_cost"], {"s": 0})
        self.assertEqual(action["sort_right_cost"], {"s": 0})
        self.assertTrue(action["reused_existing_order"]["left"])
        self.assertTrue(action["reused_existing_order"]["right"])
        self.assertTrue(verify_strategy(problem, strategy).ok)

    def test_global_reuse_order_false_forces_sort_fee(self) -> None:
        data = two_table_problem()
        data["reuse_existing_order"] = False
        problem = parse_problem(data)
        strategy = plan_strategy(problem)
        self.assertEqual(strategy["objective"]["worst_total_cost"], 25)
        node_id = strategy["nodes"][strategy["root_node"]]["branches"][0]["next_node"]
        action = strategy["nodes"][node_id]["action"]
        self.assertTrue(action["sort_left_required"])
        self.assertTrue(action["sort_right_required"])
        report = verify_strategy(problem, strategy, check_optimal=True)
        self.assertTrue(report.ok, report.errors)

    def test_missing_order_can_sort_when_sort_table_declared(self) -> None:
        data = two_table_problem()
        data["tables"] = ["A", "B"]
        problem = parse_problem(data)
        strategy = plan_strategy(problem)
        # merge fee 1 + left sort 3+2*4=11 + right sort 3+2*5=13
        self.assertEqual(strategy["objective"]["worst_total_cost"], 25)
        action = strategy["nodes"][strategy["root_node"]]["branches"][0]["next_node"]
        action = strategy["nodes"][action]
        self.assertEqual(action["action"]["algorithm"], "merge")
        self.assertTrue(action["action"]["sort_left_required"])
        self.assertTrue(action["action"]["sort_right_required"])
        report = verify_strategy(problem, strategy, check_optimal=True)
        self.assertTrue(report.ok, report.errors)

    def test_missing_order_without_sort_table_falls_back_to_hash(self) -> None:
        data = two_table_problem()
        data["tables"] = ["A", "B"]
        del data["cost_model"]["sort_cost"]
        problem = parse_problem(data)
        strategy = plan_strategy(problem)
        self.assertEqual(strategy["objective"]["worst_total_cost"], 100)
        node_id = strategy["nodes"][strategy["root_node"]]["branches"][0]["next_node"]
        self.assertEqual(strategy["nodes"][node_id]["action"]["algorithm"], "hash")
        self.assertTrue(verify_strategy(problem, strategy).ok)


class MemoryBoundaryTests(unittest.TestCase):
    def test_join_memory_limit_is_an_exact_boundary(self) -> None:
        data = two_table_problem()
        data["cost_model"]["join_costs"]["hash"] = {
            "default_cost": 1,
            "default_memory": 9,
        }
        data["cost_model"]["join_costs"]["merge"] = {"fixed": 50, "default_memory": 1}

        data["edges"][0]["memory_limit"] = 9
        at_limit = parse_problem(copy.deepcopy(data))
        strategy = plan_strategy(at_limit)
        action_id = strategy["nodes"][strategy["root_node"]]["branches"][0]["next_node"]
        self.assertEqual(
            strategy["nodes"][action_id]["action"]["algorithm"], "hash"
        )
        self.assertEqual(strategy["objective"]["worst_total_cost"], 1)
        self.assertTrue(verify_strategy(at_limit, strategy).ok)

        data["edges"][0]["memory_limit"] = 8
        under_limit = parse_problem(data)
        strategy = plan_strategy(under_limit)
        action_id = strategy["nodes"][strategy["root_node"]]["branches"][0]["next_node"]
        action = strategy["nodes"][action_id]["action"]
        self.assertEqual(action["algorithm"], "merge")
        self.assertEqual(strategy["objective"]["worst_total_cost"], 50)
        report = verify_strategy(under_limit, strategy, check_optimal=True)
        self.assertTrue(report.ok, report.errors)

    def test_sort_memory_one_row_over_block_limit_falls_back_to_hash(self) -> None:
        data = two_table_problem()
        # Remove pre-declared order so both sides must sort.
        data["tables"] = [
            {"name": "A", "memory_limit": 3},  # needs 4 units for 4 rows
            {"name": "B", "memory_limit": 5},  # exact boundary for 5 rows
        ]
        blocked = parse_problem(data)
        strategy = plan_strategy(blocked)
        action_id = strategy["nodes"][strategy["root_node"]]["branches"][0]["next_node"]
        self.assertEqual(
            strategy["nodes"][action_id]["action"]["algorithm"], "hash"
        )
        self.assertEqual(strategy["objective"]["worst_total_cost"], 100)
        self.assertTrue(verify_strategy(blocked, strategy).ok)

        data["tables"][0]["memory_limit"] = 4
        exact = parse_problem(data)
        strategy = plan_strategy(exact)
        self.assertEqual(strategy["objective"]["worst_total_cost"], 25)
        report = verify_strategy(exact, strategy, check_optimal=True)
        self.assertTrue(report.ok, report.errors)

    def _problem_with_b_limit(self, limit: int) -> object:
        data = copy.deepcopy(probe_example())
        data["tables"] = [
            {"name": "A"},
            {"name": "B", "memory_limit": limit},
            {"name": "C"},
        ]
        return parse_problem(data)

    def test_probe_memory_exactly_at_limit_is_allowed(self) -> None:
        problem = self._problem_with_b_limit(20)
        strategy = plan_strategy(problem)
        self.assertEqual(strategy["objective"]["worst_total_cost"], 161)
        first_id = strategy["nodes"][strategy["root_node"]]["branches"][0]["next_node"]
        action = strategy["nodes"][first_id]["action"]
        self.assertEqual((action["kind"], action["subset"]), ("materialize", "B"))
        report = verify_strategy(problem, strategy, check_optimal=True)
        self.assertTrue(report.ok, report.errors)

    def test_probe_memory_one_row_over_limit_is_blocked(self) -> None:
        problem = self._problem_with_b_limit(19)
        strategy = plan_strategy(problem)
        first_id = strategy["nodes"][strategy["root_node"]]["branches"][0]["next_node"]
        first_action = strategy["nodes"][first_id]["action"]
        self.assertNotEqual(first_action.get("subset"), "B")
        # C probe gives worst 205 in the supplied integer model.
        self.assertEqual(strategy["objective"]["worst_total_cost"], 205)
        report = verify_strategy(problem, strategy, check_optimal=True)
        self.assertTrue(report.ok, report.errors)


class ExistingMaterializedBlockTests(unittest.TestCase):
    def test_initial_observation_permits_different_first_actions(self) -> None:
        data = copy.deepcopy(probe_example())
        data["seed_blocks"] = [{"tables": ["B"], "materialized": True}]
        problem = parse_problem(data)
        strategy = plan_strategy(problem)

        # No B materialization fee: scenario totals are 105 and 141.
        self.assertEqual(strategy["objective"]["worst_total_cost"], 141)
        self.assertEqual(strategy["objective"]["sum_of_scenario_total_costs"], 246)
        root = strategy["nodes"]["root"]
        self.assertEqual(len(root["branches"]), 2)

        for sid in ("small-A", "small-C"):
            sw = strategy["switches"][sid][0]
            self.assertEqual(sw["location"], "initial_materialized_block")
            self.assertTrue(sw["causes_switch"])

        report = verify_strategy(problem, strategy, check_optimal=True)
        self.assertTrue(report.ok, report.errors)

    def test_disallow_reusing_materialized_block_requires_new_probe(self) -> None:
        data = copy.deepcopy(probe_example())
        data["seed_blocks"] = [{"tables": ["B"], "materialized": True}]
        data["reuse_materialized_blocks"] = False
        problem = parse_problem(data)
        strategy = plan_strategy(problem)

        # Declared materialization is not reused as an initial observation;
        # paying the common B probe restores the 161 worst-case strategy.
        self.assertEqual(strategy["objective"]["worst_total_cost"], 161)
        first_id = strategy["nodes"][strategy["root_node"]]["branches"][0]["next_node"]
        action = strategy["nodes"][first_id]["action"]
        self.assertEqual((action["kind"], action["subset"]), ("materialize", "B"))
        report = verify_strategy(problem, strategy, check_optimal=True)
        self.assertTrue(report.ok, report.errors)


class NoRecomputationTests(unittest.TestCase):
    def test_validator_rejects_join_using_consumed_child(self) -> None:
        problem = parse_problem(copy.deepcopy(probe_example()))
        strategy = plan_strategy(problem)
        tampered = copy.deepcopy(strategy)

        # Find the final A+B + C join and make it reference consumed singleton A.
        target = None
        for node in tampered["nodes"].values():
            action = node.get("action")
            if (
                isinstance(action, dict)
                and action.get("kind") == "join"
                and action.get("left_subset") == "A+B"
                and action.get("right_subset") == "C"
            ):
                target = node
                break
        self.assertIsNotNone(target)
        target["action"]["left_mask"] = 1  # singleton A is no longer frontier

        report = verify_strategy(problem, tampered)
        self.assertFalse(report.ok)
        self.assertTrue(
            any("not on the current frontier" in e for e in report.errors),
            msg=report.errors,
        )


class SixTableBoundaryTests(unittest.TestCase):
    def test_six_table_chain_is_supported_and_verified(self) -> None:
        names = [f"T{i}" for i in range(6)]
        rows = {}
        for mask in range(1, 1 << 6):
            subset = "+".join(names[i] for i in range(6) if mask & (1 << i))
            rows[subset] = mask.bit_count()
        data = {
            "tables": names,
            "edges": [
                {"left": names[i], "right": names[i + 1]}
                for i in range(5)
            ],
            "scenarios": [{"id": "only", "rows": rows}],
            "cost_model": {
                "join_costs": {
                    "hash": {"per_left_row": 1, "per_right_row": 1},
                    "merge": {"default_cost": 10_000},
                },
                "materialization_cost": {"fixed": 100},
            },
            "default_memory_limit": 10_000,
        }
        problem = parse_problem(data)
        strategy = plan_strategy(problem)
        self.assertEqual(strategy["objective"]["worst_total_cost"],
                         strategy["objective"]["scenario_total_costs"]["only"])
        report = verify_strategy(problem, strategy, check_optimal=True)
        self.assertTrue(report.ok, report.errors)

    def test_more_than_six_tables_rejected(self) -> None:
        data = {
            "tables": [f"T{i}" for i in range(7)],
            "edges": [],
            "scenarios": [{"id": "s", "rows": {"T0": 1}}],
        }
        with self.assertRaises(Exception):
            parse_problem(data)


if __name__ == "__main__":
    unittest.main()
