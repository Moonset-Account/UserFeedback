"""The reproducible verification input from the problem statement.

Total <= 200, contains negative noisy readings and a fully missing branch.
Callers can replay this example and check both the total cost and each level's
total without touching any individual-level record.
"""

import unittest

from hierpost import Hierarchy, solve_spec
from hierpost.example import SPEC
from hierpost.reference import brute_force


class ExampleInputTests(unittest.TestCase):
    def test_total_within_limit(self):
        self.assertLessEqual(SPEC["root_total"], 200)

    def test_has_negative_noise(self):
        negatives = [
            nid
            for nid, n in SPEC["nodes"].items()
            if "noisy" in n and n["noisy"] < 0
        ]
        self.assertTrue(negatives)
        self.assertIn("urban_east", negatives)

    def test_has_missing_branch(self):
        missing = [
            nid for nid, n in SPEC["nodes"].items() if "noisy" not in n
        ]
        # a whole subtree: the branch node and both of its leaves
        self.assertIn("rural", missing)
        self.assertIn("rural_north", missing)
        self.assertIn("rural_south", missing)

    def test_expected_optimum(self):
        result = solve_spec(SPEC)
        # Cross-checked against exhaustive enumeration of every valid table.
        self.assertEqual(result.total_cost, 31544)
        self.assertEqual(result.leaf_vector, (74, 70, 0, 56))

    def test_matches_brute_force(self):
        h = Hierarchy.from_dict(SPEC)
        cost, vector = brute_force(h)
        result = solve_spec(SPEC)
        self.assertEqual(result.total_cost, cost)
        self.assertEqual(result.leaf_vector, vector)

    def test_additivity_and_root_total(self):
        result = solve_spec(SPEC)
        self.assertEqual(result.counts["root"], 200)
        self.assertEqual(
            result.counts["urban"],
            result.counts["urban_east"] + result.counts["urban_west"],
        )
        self.assertEqual(
            result.counts["rural"],
            result.counts["rural_north"] + result.counts["rural_south"],
        )
        self.assertEqual(
            result.counts["root"],
            result.counts["urban"] + result.counts["rural"],
        )

    def test_level_totals_are_reproducible(self):
        result = solve_spec(SPEC)
        self.assertEqual(result.level_totals, {0: 200, 1: 200, 2: 200})

    def test_every_published_value_non_negative(self):
        result = solve_spec(SPEC)
        for value in result.counts.values():
            self.assertGreaterEqual(value, 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
