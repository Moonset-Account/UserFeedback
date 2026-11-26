import unittest

from transfer_approval.intervals import (
    Interval,
    add_intervals,
    intersect,
    merge_intervals,
    subtract,
    union_minus_intersection,
)


class IntervalAlgebraTests(unittest.TestCase):
    def test_closed_intersection_and_point_boundary(self):
        a = Interval(1, 3)
        b = Interval(3, 7)
        result = intersect(a, b)
        self.assertEqual(result, Interval.point(3))
        self.assertTrue(result.contains(3))

    def test_open_boundary_intersection_is_empty(self):
        a = Interval(1, 3, end_inclusive=False)
        b = Interval(3, 7, start_inclusive=False)
        self.assertTrue(intersect(a, b).empty)

    def test_delay_sum_keeps_max_delay_boundary(self):
        send = Interval(3, 3)
        reachable = add_intervals(send, Interval(0, 4, True, True))
        self.assertEqual(reachable, Interval(3, 7))
        self.assertTrue(reachable.contains(7))

    def test_merge_half_open_adjacent_intervals(self):
        merged = merge_intervals(
            [Interval(0, 5, True, False), Interval(5, 10, True, False)]
        )
        self.assertEqual(merged, [Interval(0, 10, True, False)])

    def test_do_not_merge_when_shared_endpoint_is_excluded_by_both(self):
        merged = merge_intervals(
            [Interval(0, 5, True, False), Interval(5, 10, False, True)]
        )
        self.assertEqual(len(merged), 2)
        self.assertFalse(merged[0].contains(5))
        self.assertFalse(merged[1].contains(5))

    def test_subtract_closed_window_splits_interval(self):
        pieces = subtract(Interval(0, 10), Interval(3, 7))
        self.assertEqual(pieces, [Interval(0, 3, True, False), Interval(7, 10, False, True)])

    def test_union_minus_common_shows_partial_coverage(self):
        partial = union_minus_intersection([Interval(0, 6), Interval(4, 10)])
        self.assertEqual(
            partial,
            [Interval(0, 4, True, False), Interval(6, 10, False, True)],
        )


if __name__ == "__main__":
    unittest.main()
