"""Constraint and correctness tests for the post-processing solver."""

import unittest

from hierpost import Hierarchy, Node, solve, solve_spec
from hierpost.reference import brute_force


def build(spec_nodes, root_total):
    return Hierarchy(
        [
            Node(
                node_id=nid,
                parent=p,
                weight=w,
                noisy=y,
                leaf_index=li,
            )
            for nid, p, w, y, li in spec_nodes
        ],
        root_total,
    )


def assert_valid_table(testcase, hierarchy, result):
    """Every hard constraint from the problem statement."""
    counts = result.counts
    # non-negativity
    for nid, value in counts.items():
        testcase.assertIsInstance(value, int)
        testcase.assertGreaterEqual(value, 0, f"{nid} negative")
    # root total
    testcase.assertEqual(counts[hierarchy.root], hierarchy.root_total)
    # additivity
    for nid, children in hierarchy.children.items():
        if children:
            testcase.assertEqual(
                counts[nid], sum(counts[c] for c in children), nid
            )
    # reported cost matches an independent recomputation
    independent = sum(
        n.weight * (counts[nid] - n.noisy) ** 2
        for nid, n in hierarchy.nodes.items()
        if n.noisy is not None
    )
    testcase.assertEqual(result.total_cost, independent)


class SolverConstraintTests(unittest.TestCase):
    def test_root_only(self):
        h = build([("root", None, 2, 3, 0)], 7)
        r = solve(h)
        assert_valid_table(self, h, r)
        self.assertEqual(r.counts, {"root": 7})
        self.assertEqual(r.total_cost, 2 * (7 - 3) ** 2)

    def test_total_zero_forces_all_zero(self):
        h = build(
            [
                ("root", None, 1, 3, None),
                ("a", "root", 1, -2, 0),
                ("b", "root", 1, 9, 1),
            ],
            0,
        )
        r = solve(h)
        assert_valid_table(self, h, r)
        self.assertEqual(r.leaf_vector, (0, 0))
        self.assertEqual(r.total_cost, 9 + 4 + 81)

    def test_all_missing_distributes_mass(self):
        h = build(
            [
                ("root", None, None, None, None),
                ("a", "root", None, None, 0),
                ("b", "root", None, None, 1),
            ],
            5,
        )
        r = solve(h)
        assert_valid_table(self, h, r)
        self.assertEqual(r.total_cost, 0)
        # No measurement -> tie -> lex-smallest leaf vector is (0, 5).
        self.assertEqual(r.leaf_vector, (0, 5))

    def test_negative_noise_is_clamped_to_non_negative_counts(self):
        h = build(
            [
                ("root", None, 1, -50, None),
                ("a", "root", 5, -50, 0),
                ("b", "root", 1, 0, 1),
            ],
            4,
        )
        r = solve(h)
        assert_valid_table(self, h, r)
        # a would like to be -50 but must be >= 0 with everything summing to 4,
        # so mass is pushed to b only when it helps; costs stay finite.
        self.assertGreaterEqual(r.counts["a"], 0)

    def test_level_totals_equal_root_for_complete_levels(self):
        h = build(
            [
                ("root", None, 1, 205, None),
                ("u", "root", 1, 100, None),
                ("r", "root", 1, 90, None),
                ("u1", "u", 1, -2, 0),
                ("u2", "u", 1, 60, 1),
                ("r1", "r", 1, 40, 2),
                ("r2", "r", 1, 40, 3),
            ],
            200,
        )
        r = solve(h)
        assert_valid_table(self, h, r)
        self.assertEqual(r.level_totals[0], 200)  # root
        self.assertEqual(r.level_totals[1], 200)  # u + r cover the root
        self.assertEqual(r.level_totals[2], 200)  # all leaves

    def test_unmeasured_branch_still_receives_people(self):
        h = build(
            [
                ("root", None, 1, 100, None),
                ("m", "root", 1, 100, None),
                ("miss", "root", None, None, None),
                ("m1", "m", 1, 40, 0),
                ("m2", "m", 1, 40, 1),
                ("x1", "miss", None, None, 2),
                ("x2", "miss", None, None, 3),
            ],
            100,
        )
        r = solve(h)
        assert_valid_table(self, h, r)
        self.assertEqual(r.counts["m"] + r.counts["miss"], 100)
        # The unmeasured branch carries people but adds no error terms.
        self.assertNotIn("miss", r.measured_cost)
        self.assertNotIn("x1", r.measured_cost)

    def test_partial_depth_level_total_is_less_than_root(self):
        # Mixed leaf depth: "b" is a leaf at depth 1 while "a" is internal with
        # leaves a1, a2 at depth 2.  The depth-2 total covers only a1 + a2, so
        # it equals the root total minus the mass published at the shallower
        # leaf "b".
        h = build(
            [
                ("root", None, 1, 100, None),
                ("a", "root", 1, 50, None),
                ("b", "root", 1, 50, 0),
                ("a1", "a", 1, 20, 1),
                ("a2", "a", 1, 20, 2),
            ],
            100,
        )
        r = solve(h)
        assert_valid_table(self, h, r)
        self.assertEqual(r.level_totals[0], 100)  # root
        self.assertEqual(r.level_totals[1], 100)  # a + b
        # depth 2 = a1 + a2 = 100 - count(b)
        self.assertEqual(r.level_totals[2], 100 - r.counts["b"])
        self.assertLess(r.level_totals[2], 100)


class SolverOptimalityTests(unittest.TestCase):
    """Cross-check the DP against exhaustive enumeration on small trees."""

    def _compare(self, hierarchy):
        result = solve(hierarchy)
        best_cost, best_vec = brute_force(hierarchy)
        assert_valid_table(self, hierarchy, result)
        self.assertEqual(result.total_cost, best_cost)
        self.assertEqual(result.leaf_vector, best_vec)

    def test_example_spec_matches_brute_force(self):
        from hierpost.example import SPEC

        self._compare(Hierarchy.from_dict(SPEC))

    def test_tie_breaks_to_lexicographically_smallest(self):
        # Two symmetric leaves at noisy 0, total 1: (0,1) and (1,0) tie.
        h = build(
            [
                ("root", None, None, None, None),
                ("l0", "root", 1, 0, 0),
                ("l1", "root", 1, 0, 1),
            ],
            1,
        )
        r = solve(h)
        self.assertEqual(r.leaf_vector, (0, 1))
        self.assertEqual(r.total_cost, 1)

    def test_leaf_index_order_controls_tie_break(self):
        # leaf_index deliberately out of id order: l1 has index 0.
        h = build(
            [
                ("root", None, None, None, None),
                ("l0", "root", 1, 0, 1),
                ("l1", "root", 1, 0, 0),
            ],
            1,
        )
        r = solve(h)
        self.assertEqual(h.leaves, ["l1", "l0"])
        self.assertEqual(r.leaf_vector, (0, 1))  # first in leaf_index order is 0
        self.assertEqual(r.counts["l1"], 0)
        self.assertEqual(r.counts["l0"], 1)

    def test_exhaustive_small_shapes(self):
        shapes = [
            # (nodes, total)
            (
                [
                    ("root", None, 1, 30, None),
                    ("a", "root", 2, -5, 0),
                    ("b", "root", 1, 20, 1),
                ],
                8,
            ),
            (
                [
                    ("root", None, 1, 40, None),
                    ("p", "root", 1, 15, None),
                    ("q", "root", 3, 20, None),
                    ("p1", "p", 1, 5, 0),
                    ("p2", "p", 2, 10, 1),
                    ("q1", "q", 1, 12, 2),
                ],
                9,
            ),
            (
                [
                    ("root", None, 1, 9, None),
                    ("a", "root", 1, None, None),
                    ("b", "root", 1, 4, None),
                    ("a1", "a", 1, -3, 0),
                    ("a2", "a", 1, 7, 1),
                    ("b1", "b", 1, 4, 2),
                    ("b2", "b", 1, 1, 3),
                ],
                7,
            ),
        ]
        for nodes, total in shapes:
            with self.subTest(total=total):
                self._compare(build(nodes, total))


class SolverCostReportingTests(unittest.TestCase):
    def test_measured_cost_breakdown_sums_to_total(self):
        from hierpost.example import SPEC

        r = solve_spec(SPEC)
        self.assertEqual(sum(r.measured_cost.values()), r.total_cost)
        self.assertEqual(
            set(r.measured_cost),
            {
                nid
                for nid, n in SPEC["nodes"].items()
                if "noisy" in n
            },
        )

    def test_level_totals_sum_over_nodes(self):
        from hierpost.example import SPEC

        h = Hierarchy.from_dict(SPEC)
        r = solve(h)
        for depth, expected in r.level_totals.items():
            actual = sum(
                r.counts[nid]
                for nid, d in h.depth.items()
                if d == depth
            )
            self.assertEqual(actual, expected)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
