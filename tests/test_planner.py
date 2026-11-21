"""Acceptance tests for the cost-based left-deep planner.

Covers:
* an acceptable optimum and its exact step-by-step cost breakdown,
* rejected alternatives (broadcast over the limit, forbidden unconnected
  extensions, expensive full repartition) that the planner must not pick,
* boundary/interleaved cases: a slightly more expensive intermediate that
  is already partitioned on the next key wins globally, and the broadcast
  limit boundary flips the chosen method.
"""

from __future__ import annotations

import unittest

from joinplan import parse_query, plan_query
from joinplan.parse import connectable_sets

from reference_planner import assert_plan_shape, best_cost


def make_query(tables, edges, outputs, nodes=4, broadcast_limit=None):
    """Build a validated query, auto-filling any unlisted output estimate.

    The parser demands an estimate for *every* connected table set, so tests
    only spell out the estimates their assertions depend on; the rest get a
    deterministic fallback (sum of member table sizes) that every consumer
    of the same ``Query`` agrees on.
    """
    table_by_name = {table["name"]: table for table in tables}
    edge_objects = [{"left": left, "right": right} for left, right in edges]
    provided = {frozenset(names): size for names, size in outputs}
    complete = dict(provided)
    for group in connectable_sets(table_by_name, _edge_views(edge_objects)):
        if group not in complete:
            complete[group] = sum(table_by_name[name]["bytes"] for name in group)

    request = {
        "nodes": nodes,
        "tables": tables,
        "edges": edge_objects,
        "outputs": [
            {"tables": sorted(group), "bytes": size}
            for group, size in complete.items()
        ],
    }
    if broadcast_limit is not None:
        request["broadcast_limit"] = broadcast_limit
    return parse_query(request)


def _edge_views(edges):
    """Minimal edge objects for :func:`connectable_sets`."""
    from joinplan.model import JoinEdge

    return [JoinEdge(left=edge["left"], right=edge["right"]) for edge in edges]


class BasicOptimumTests(unittest.TestCase):
    def test_broadcast_beats_repartition_for_a_tiny_right_table(self):
        # A(1000) joins B(100): broadcast B costs 100*3 = 300 on 4 nodes;
        # repartitioning costs up to 1100.  The plan must broadcast.
        query = make_query(
            tables=[
                {"name": "A", "bytes": 1000},
                {"name": "B", "bytes": 100},
            ],
            edges=[("A.k", "B.k")],
            outputs=[(["A", "B"], 2000)],
        )
        plan = plan_query(query)
        self.assertEqual(plan.total_cost, 300)
        self.assertEqual(len(plan.steps), 1)
        step = plan.steps[0]
        self.assertEqual(step.method, "broadcast")
        self.assertEqual(step.right_table, "B")
        self.assertEqual(step.cost, 300)
        self.assertEqual(step.cumulative_cost, 300)
        self.assertEqual(step.result_tables, frozenset({"A", "B"}))
        assert_plan_shape(self, plan, query)
        self.assertEqual(plan.total_cost, best_cost(query))

    def test_partitioned_left_and_right_makes_repartition_free(self):
        # A already partitioned on k, B already partitioned on k: repartition
        # moves zero bytes, so it must be strictly cheaper than broadcasting.
        query = make_query(
            tables=[
                {"name": "A", "bytes": 1000, "partition_key": "k"},
                {"name": "B", "bytes": 5000, "partition_key": "k"},
            ],
            edges=[("A.k", "B.k")],
            outputs=[(["A", "B"], 9000)],
        )
        plan = plan_query(query)
        self.assertEqual(plan.total_cost, 0)
        self.assertEqual(plan.steps[0].method, "repartition")
        self.assertEqual(plan.steps[0].left_transfer, 0)
        self.assertEqual(plan.steps[0].right_transfer, 0)
        self.assertEqual(plan.partition_columns, ("A.k", "B.k"))
        self.assertEqual(plan.total_cost, best_cost(query))

    def test_only_unpartitioned_side_is_charged(self):
        # A is partitioned on k, B is not.  Repartitioning from A charges
        # only B (1000), which must beat both broadcasting B from A (3000)
        # and starting from B (broadcast A 6000 / repartition A+B 3000).
        query = make_query(
            tables=[
                {"name": "A", "bytes": 2000, "partition_key": "k"},
                {"name": "B", "bytes": 1000},
            ],
            edges=[("A.k", "B.k")],
            outputs=[(["A", "B"], 9000)],
        )
        plan = plan_query(query)
        self.assertEqual(plan.total_cost, 1000)
        self.assertEqual(plan.steps[0].method, "repartition")
        # Two equivalent optima exist (start from A or from B); in both, the
        # only bytes moved belong to the unpartitioned table B.
        step = plan.steps[0]
        if step.left_tables == frozenset({"A"}):
            self.assertEqual((step.left_transfer, step.right_transfer), (0, 1000))
        else:
            self.assertEqual(step.left_tables, frozenset({"B"}))
            self.assertEqual((step.left_transfer, step.right_transfer), (1000, 0))
        self.assertEqual(plan.total_cost, best_cost(query))


class RejectedAlternativesTests(unittest.TestCase):
    def test_broadcast_over_the_limit_is_not_used(self):
        # B is small enough to look attractive but exceeds the 50-byte cap.
        query = make_query(
            tables=[
                {"name": "A", "bytes": 1000, "partition_key": "k"},
                {"name": "B", "bytes": 100},
            ],
            edges=[("A.k", "B.k")],
            outputs=[(["A", "B"], 2000)],
            broadcast_limit=50,
        )
        plan = plan_query(query)
        self.assertEqual({step.method for step in plan.steps}, {"repartition"})
        # A is already partitioned, so only B moves.
        self.assertEqual(plan.total_cost, 100)
        assert_plan_shape(self, plan, query)

    def test_unconnected_extension_is_never_generated(self):
        # Star: A joins B and A joins C.  B and C share no edge, so a
        # left-deep order like B, C, A would require a cross product between
        # B and C and must be rejected; every step joins a connected table.
        query = make_query(
            tables=[
                {"name": "A", "bytes": 10},
                {"name": "B", "bytes": 10},
                {"name": "C", "bytes": 10},
            ],
            edges=[("A.k", "B.k"), ("A.j", "C.j")],
            outputs=[(["A", "B"], 10), (["A", "C"], 10), (["A", "B", "C"], 10)],
        )
        plan = plan_query(query)
        for step in plan.steps:
            edges = [
                edge
                for edge in query.edges
                if (
                    edge.left.split(".")[0] in step.left_tables
                    and edge.right.split(".")[0] == step.right_table
                )
                or (
                    edge.right.split(".")[0] in step.left_tables
                    and edge.left.split(".")[0] == step.right_table
                )
            ]
            self.assertTrue(edges, f"step {step.index} extends without a join edge")
        self.assertEqual(plan.total_cost, best_cost(query))


class InterleavedBoundaryTests(unittest.TestCase):
    def test_slightly_pricier_partitioned_intermediate_wins_globally(self):
        # Chain A - B - C.  A and B are partitioned on j, C on k.
        # Edges: A.j = B.j, A.k = B.k, B.k = C.k.
        #
        # Joining A and B on the j predicate is FREE (both already on j) and
        # yields an AB result partitioned on j -> cost 0, but not on k.
        # Joining A and B on the k predicate costs A(1000) + B(1000) = 2000
        # and yields an AB result partitioned on k.
        #
        # Final join AB - C on k:
        #   * from the j-partitioned AB: left must move (AB = 10000 bytes),
        #     C is already on k; total 0 + 10000 = 10000.
        #   * from the k-partitioned AB: nothing moves; total 2000 + 0 = 2000.
        #
        # A planner that keeps only the cheapest plan per table subset would
        # keep AB@0 (j-partitioned) and return 10000.  The correct optimum
        # deliberately pays 2000 up front and wins globally with 2000.
        query = make_query(
            tables=[
                {"name": "A", "bytes": 1000, "partition_key": "j"},
                {"name": "B", "bytes": 1000, "partition_key": "j"},
                {"name": "C", "bytes": 100000, "partition_key": "k"},
            ],
            edges=[("A.j", "B.j"), ("A.k", "B.k"), ("B.k", "C.k")],
            outputs=[(["A", "B"], 10000), (["A", "B", "C"], 10000)],
            nodes=4,
            broadcast_limit=50000,
        )
        plan = plan_query(query)
        assert_plan_shape(self, plan, query)
        self.assertEqual(plan.total_cost, best_cost(query))

        # The first step is the *more expensive* k-join, not the free j-join.
        first = plan.steps[0]
        self.assertEqual(first.method, "repartition")
        self.assertEqual(first.cost, 2000)
        # ... and the plan beats the greedy "cheapest first step" route.
        self.assertEqual(plan.total_cost, 2000)
        self.assertLess(plan.total_cost, 10000)
        # The intermediate really is partitioned on the next join key.
        self.assertEqual(first.result_partition_columns, ("A.k", "B.k"))
        self.assertEqual(plan.steps[1].cost, 0)

    def test_broadcast_limit_boundary_flips_the_method(self):
        # B is exactly at the limit -> broadcast allowed; one byte over -> not.
        at_limit = make_query(
            tables=[
                {"name": "A", "bytes": 100, "partition_key": "k"},
                {"name": "B", "bytes": 100},
            ],
            edges=[("A.k", "B.k")],
            outputs=[(["A", "B"], 100)],
            broadcast_limit=100,
        )
        plan_at = plan_query(at_limit)
        self.assertEqual(plan_at.total_cost, best_cost(at_limit))
        self.assertLessEqual(plan_at.total_cost, 100 * 3)

        over_limit = make_query(
            tables=[
                {"name": "A", "bytes": 100, "partition_key": "k"},
                {"name": "B", "bytes": 101},
            ],
            edges=[("A.k", "B.k")],
            outputs=[(["A", "B"], 100)],
            broadcast_limit=100,
        )
        plan_over = plan_query(over_limit)
        self.assertEqual({step.method for step in plan_over.steps}, {"repartition"})
        self.assertEqual(plan_over.total_cost, 101)
        self.assertEqual(plan_over.total_cost, best_cost(over_limit))


class SevenTableStressTests(unittest.TestCase):
    def test_seven_table_chain_matches_bruteforce(self):
        tables = [
            {"name": f"T{index}", "bytes": 100 + index * 7}
            for index in range(7)
        ]
        edges = [(f"T{index}.k", f"T{index + 1}.k") for index in range(6)]
        outputs = [( [f"T{index}", f"T{index + 1}"], 150 + index) for index in range(6)]
        outputs.append(([f"T{index}" for index in range(7)], 1_000))
        query = make_query(tables=tables, edges=edges, outputs=outputs, nodes=8)
        plan = plan_query(query)
        self.assertEqual(plan.total_cost, best_cost(query))
        self.assertEqual(len(plan.steps), 6)
        assert_plan_shape(self, plan, query)


if __name__ == "__main__":
    unittest.main()
