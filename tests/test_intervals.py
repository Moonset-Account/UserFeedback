"""Unit tests for the half-open interval primitives."""

import unittest

from evidence_accounting.intervals import (
    earliest_gap,
    intersect,
    intersect_lists,
    merge,
    merge_with_blocked,
    subtract,
    subtract_many,
    uncovered_parts,
)


class IntersectTests(unittest.TestCase):
    def test_partial_overlap(self):
        self.assertEqual(intersect((0, 10), (5, 15)), (5, 10))

    def test_containment(self):
        self.assertEqual(intersect((0, 10), (2, 5)), (2, 5))

    def test_touching_half_open_has_no_overlap(self):
        self.assertIsNone(intersect((0, 10), (10, 20)))

    def test_disjoint(self):
        self.assertIsNone(intersect((0, 5), (6, 10)))

    def test_intersect_lists_merges_results(self):
        result = intersect_lists([(0, 10)], [(0, 4), (3, 8)])
        self.assertEqual(result, [(0, 8)])


class SubtractTests(unittest.TestCase):
    def test_full_cover(self):
        self.assertEqual(subtract((0, 10), (0, 10)), [])
        self.assertEqual(subtract((2, 8), (0, 10)), [])

    def test_disjoint(self):
        self.assertEqual(subtract((0, 10), (10, 20)), [(0, 10)])
        self.assertEqual(subtract((0, 10), (-5, 0)), [(0, 10)])

    def test_left_removed(self):
        self.assertEqual(subtract((0, 10), (-2, 4)), [(4, 10)])

    def test_right_removed(self):
        self.assertEqual(subtract((0, 10), (7, 20)), [(0, 7)])

    def test_middle_removed_splits_interval(self):
        self.assertEqual(subtract((0, 10), (4, 6)), [(0, 4), (6, 10)])

    def test_subtract_many(self):
        self.assertEqual(
            subtract_many((0, 10), [(2, 3), (5, 7)]),
            [(0, 2), (3, 5), (7, 10)],
        )


class MergeTests(unittest.TestCase):
    def test_merge_overlapping_and_touching(self):
        self.assertEqual(
            merge([(0, 5), (4, 8), (8, 10), (12, 14)]),
            [(0, 10), (12, 14)],
        )

    def test_merge_drops_empty_and_sorts(self):
        self.assertEqual(merge([(9, 9), (3, 4), (0, 3)]), [(0, 4)])


class MergeWithBlockedTests(unittest.TestCase):
    def test_blocked_point_splits_interval(self):
        self.assertEqual(
            merge_with_blocked([(0, 10)], [5]),
            [(0, 5), (5, 10)],
        )

    def test_blocked_point_prevents_touch_merge(self):
        self.assertEqual(
            merge_with_blocked([(0, 5), (5, 10)], [5]),
            [(0, 5), (5, 10)],
        )

    def test_unblocked_touch_still_merges(self):
        self.assertEqual(
            merge_with_blocked([(0, 5), (5, 10)], [7]),
            [(0, 7), (7, 10)],
        )

    def test_blocked_at_touch_boundary_keeps_segments_separate(self):
        # Point 10 is the touching boundary; merging must not cross it.
        self.assertEqual(
            merge_with_blocked([(0, 5), (5, 10), (10, 12)], [10]),
            [(0, 10), (10, 12)],
        )

    def test_internal_blocked_point_splits_merged_interval(self):
        self.assertEqual(
            merge_with_blocked([(0, 5), (5, 10), (10, 12)], [5]),
            [(0, 5), (5, 12)],
        )


class UncoveredAndEarliestGapTests(unittest.TestCase):
    def test_uncovered_parts(self):
        self.assertEqual(
            uncovered_parts((0, 10), [(2, 4), (6, 8)]),
            [(0, 2), (4, 6), (8, 10)],
        )

    def test_fully_covered_returns_none(self):
        self.assertIsNone(earliest_gap((0, 10), [(0, 10)]))

    def test_earliest_gap_is_first_uncovered_instant(self):
        self.assertEqual(earliest_gap((0, 10), [(2, 10)]), 0)
        self.assertEqual(earliest_gap((0, 10), [(0, 4), (6, 10)]), 4)

    def test_blocked_point_is_uncovered_even_when_surrounded(self):
        self.assertEqual(
            earliest_gap((0, 10), [(0, 10)], blocked_points=[5]),
            5,
        )

    def test_blocked_point_outside_span_ignored(self):
        self.assertIsNone(
            earliest_gap((0, 10), [(0, 10)], blocked_points=[12])
        )


if __name__ == "__main__":
    unittest.main()
