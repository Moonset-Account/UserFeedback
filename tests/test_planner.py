import json
import unittest
from decimal import Decimal

from probe_planner import (
    InfeasibleCoverageError,
    MISSING,
    FAILED,
    ProbePlacementPlanner,
    build_probes,
    plan_placement,
    plan_probes,
    solve,
)


class PlannerBehaviorTests(unittest.TestCase):
    def test_site_outage_and_shared_path_dependency_are_not_equivalent(self):
        # Both probes are from the same site and cross the same dependency.
        # Under site loss both are missing; under dependency failure both send
        # failure receipts.  Those vectors must remain distinguishable.
        probes = [
            {"id": "a-e1", "site": "a", "entry": "e1", "dependencies": ["d"]},
            {"id": "a-e2", "site": "a", "entry": "e2", "dependencies": ["d"]},
        ]
        faults = [
            {"id": "site-a", "kind": "site", "site": "a"},
            {"id": "dep-d", "kind": "dependency", "dependency": "d"},
        ]

        result = plan_probes(probes, faults, budget=2, required_entries=["e1", "e2"])

        self.assertEqual(result.selected_ids, ("a-e1", "a-e2"))
        self.assertEqual(result.signatures["site-a"], (MISSING, MISSING))
        self.assertEqual(result.signatures["dep-d"], (FAILED, FAILED))
        self.assertEqual(result.distinguished_pair_count, 1)
        self.assertEqual(result.ambiguous_pairs, ())

    def test_interleaved_sites_entries_and_dependency_paths(self):
        probes = [
            {"id": "p1", "site": "a", "entry": "e1", "dependencies": ["d1", "d2"]},
            {"id": "p2", "site": "a", "entry": "e2", "dependencies": ["d2", "d3"]},
            {"id": "p3", "site": "b", "entry": "e1", "dependencies": ["d1", "d3"]},
        ]
        faults = ["site:a", "site:b", "d1", "d2", "d3"]

        result = plan_probes(
            probes,
            faults,
            budget=2,
            required_entries=["e1", "e2"],
        )

        # p1+p2 is the earliest candidate-order optimal cover; even though both
        # probes share site a, that common missing pattern is part of their
        # signatures and all five scenarios remain distinct.
        self.assertEqual(result.selected_ids, ("p1", "p2"))
        self.assertEqual(result.pair_count, 10)
        self.assertEqual(result.distinguished_pair_count, 10)
        self.assertEqual(result.ambiguous_pairs, ())
        self.assertEqual(result.signatures["site:a"], (MISSING, MISSING))
        self.assertEqual(result.signatures["site:b"], ("ok", "ok"))

    def test_identical_fault_effects_remain_an_ambiguous_pair(self):
        probes = [
            {"id": "p", "site": "a", "entry": "e", "dependencies": ["d1", "d2"]}
        ]
        result = plan_probes(
            probes,
            ["d1", "d2"],
            budget=1,
            required_entries=["e"],
        )

        self.assertEqual(result.selected_ids, ("p",))
        self.assertEqual(result.ambiguous_pairs, (("d1", "d2"),))
        self.assertEqual(result.distinguished_pair_count, 0)
        self.assertEqual(result.equivalence_classes, (("d1", "d2"),))

    def test_coverage_constraint_takes_priority_over_high_score_optional_probe(self):
        probes = [
            {
                "id": "required-cover",
                "site": "a",
                "entry": "critical",
                "dependencies": ["d0"],
                "cost": 3,
            },
            {
                "id": "high-score-optional",
                "site": "b",
                "entry": "optional",
                "dependencies": ["d1", "d2", "d3"],
                "cost": 2,
            },
        ]
        faults = ["d0", "d1", "d2", "d3", "site:b"]

        result = plan_probes(
            probes,
            faults,
            budget=4,
            required_entries=["critical"],
        )

        self.assertEqual(result.selected_ids, ("required-cover",))
        self.assertIn("critical", result.covered_entries)
        self.assertNotIn("optional", result.covered_entries)
        self.assertLess(result.distinguished_pair_count, 10)

    def test_maximum_pair_discrimination_under_budget(self):
        probes = [
            {"id": "p0", "site": "s0", "entry": "e0", "dependencies": ["d0"], "cost": 1},
            {"id": "p1", "site": "s1", "entry": "e1", "dependencies": ["d1"], "cost": 1},
            {"id": "p2", "site": "s2", "entry": "e2", "dependencies": ["d2"], "cost": 2},
        ]
        faults = ["d0", "d1", "d2", "d3"]

        # e0 is mandatory.  Budget 3 permits p0 + either p1 or p2.  p1 costs 1
        # and p2 costs 2, but each single-resource probe distinguishes three
        # pairs, so p0+p2 and p0+p1 have the same score.  The lower-cost plan
        # is p0+p1.
        result = plan_probes(
            probes,
            faults,
            budget=3,
            required_entries=["e0"],
        )
        self.assertEqual(result.selected_ids, ("p0", "p1"))
        self.assertEqual(result.total_cost, 2)
        self.assertEqual(result.distinguished_pair_count, 5)
        self.assertEqual(result.ambiguous_pairs, (("d2", "d3"),))

        # With budget 4, p0+p2 costs 3 and gives the same five pairs; p0+p1+p2
        # costs 4 and distinguishes all six pairs.
        richer = plan_probes(
            probes,
            faults,
            budget=4,
            required_entries=["e0"],
        )
        self.assertEqual(richer.selected_ids, ("p0", "p1", "p2"))
        self.assertEqual(richer.distinguished_pair_count, 6)
        self.assertEqual(richer.ambiguous_pairs, ())

    def test_tie_break_prefers_earlier_then_lower_cost(self):
        equal_probes = [
            {"id": "later", "site": "b", "entry": "e", "dependencies": ["d"]},
            {"id": "earlier-id-but-second", "site": "a", "entry": "e", "dependencies": ["d"]},
        ]
        result = plan_probes(equal_probes, ["d", "site:a"], budget=1)
        self.assertEqual(result.selected_ids, ("later",))

        different_cost = [
            {"id": "expensive-first", "site": "b", "entry": "e", "cost": 2},
            {"id": "cheap-second", "site": "a", "entry": "e", "cost": 1},
        ]
        priced = plan_probes(different_cost, [], budget=2)
        self.assertEqual(priced.selected_ids, ("cheap-second",))
        self.assertEqual(priced.total_cost, 1)

    def test_infeasible_unknown_entry_and_insufficient_budget(self):
        with self.assertRaises(InfeasibleCoverageError) as ctx:
            plan_probes([], [], required_entries=["missing"])
        self.assertEqual(ctx.exception.missing_entries, frozenset({"missing"}))

        probes = [{"id": "p", "site": "a", "entry": "e", "cost": 5}]
        with self.assertRaises(InfeasibleCoverageError) as ctx:
            plan_probes(probes, [], budget=3, required_entries=["e"])
        self.assertEqual(ctx.exception.minimum_cost, 5)
        self.assertEqual(ctx.exception.budget, 3)

    def test_empty_and_zero_budget_boundaries(self):
        empty = plan_probes([], [], budget=10)
        self.assertEqual(empty.selected_ids, ())
        self.assertEqual(empty.pair_count, 0)
        self.assertEqual(empty.ambiguous_pairs, ())
        self.assertEqual(empty.equivalence_classes, ())

        probes = [{"id": "free", "site": "a", "entry": "e", "cost": 0}]
        zero_budget = plan_probes(probes, [], budget=0, required_entries=["e"])
        self.assertEqual(zero_budget.selected_ids, ("free",))
        self.assertEqual(zero_budget.total_cost, 0)

        # A bare string is one entry/fault/id, not a sequence of characters.
        string_boundary = plan_probes(
            {"id": "p", "site": "a", "entry": "checkout"},
            "site:a",
            budget=1,
            required_entries="checkout",
        )
        self.assertEqual(string_boundary.selected_ids, ("p",))
        self.assertEqual(string_boundary.ambiguous_pairs, ())

    def test_decimal_costs_are_preserved(self):
        probes = [{"id": "p", "site": "a", "entry": "e", "cost": Decimal("1.50")}]
        result = plan_probes(probes, [], budget=Decimal("2.00"), required_entries=["e"])
        self.assertEqual(result.selected_ids, ("p",))
        self.assertEqual(result.total_cost, Decimal("1.50"))
        self.assertEqual(json.loads(json.dumps(result.to_dict()))["total_cost"], 1.5)

    def test_build_probes_nested_paths_and_solve_dict(self):
        probes = build_probes(
            ["bj", "sh"],
            ["checkout"],
            paths={
                "bj": {"checkout": ["edge", "auth"]},
                "sh": {"checkout": ["edge", "auth"]},
            },
            request_cost=2,
        )
        self.assertEqual([p.id for p in probes], ["bj->checkout", "sh->checkout"])
        self.assertTrue(all(p.cost == 2 for p in probes))

        payload_result = solve(
            {
                "sites": ["bj", "sh"],
                "entries": ["checkout"],
                "paths": {
                    "bj": {"checkout": ["auth"]},
                    "sh": {"checkout": ["auth"]},
                },
                "faults": ["site:bj", "dep:auth"],
                "budget": 2,
                "request_cost": 1,
            }
        )
        self.assertEqual(payload_result["selected_ids"], ["bj->checkout"])
        self.assertEqual(payload_result["ambiguous_pairs"], [])
        self.assertEqual(payload_result["required_entries"], ["checkout"])

    def test_plan_placement_alias_and_default_required_entries(self):
        result = plan_placement(
            probes=[
                ("a", "e1", ["d1"], 1),
                ("b", "e2", ["d2"], 1),
            ],
            faults=["d1", "d2"],
            budget=2,
        )
        self.assertEqual(set(result.required_entries), {"e1", "e2"})
        self.assertEqual(len(result.selected_probes), 2)
        self.assertEqual(result.ambiguous_pairs, ())

    def test_stateful_planner_infers_site_faults_and_json_output(self):
        planner = ProbePlacementPlanner(
            probes=[
                {"id": "p1", "site": "bj", "entry": "checkout", "dependencies": ["auth"]},
                {"id": "p2", "site": "sh", "entry": "search", "dependencies": ["catalog"]},
            ],
            faults=["bj", "auth", "catalog"],
            required_entries=["checkout", "search"],
            budget=2,
        )
        result = planner.plan()
        self.assertEqual(result.signatures["bj"][0], MISSING)
        self.assertEqual(result.distinguished_pair_count, 3)

        payload = solve(
            {
                "probes": [
                    {"id": "p1", "site": "bj", "entry": "checkout", "cost": 1},
                ],
                "faults": ["bj"],
                "required_entries": ["checkout"],
                "budget": 1,
            }
        )
        # Plain output must be JSON serializable.
        encoded = json.dumps(payload, ensure_ascii=False)
        self.assertIn("selected_ids", encoded)
        self.assertEqual(payload["ambiguous_pairs"], [])

        # Compatibility entry points import and expose the same core call.
        import probe_placement
        import solution

        self.assertIs(solution.plan_probes, plan_probes)
        self.assertTrue(hasattr(probe_placement, "Planner"))


if __name__ == "__main__":
    unittest.main()
