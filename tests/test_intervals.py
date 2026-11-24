"""半开区间工具的单元测试。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from temporal_fk.intervals import merge_intervals, uncovered_parts, validate_interval  # noqa: E402


class TestValidateInterval(unittest.TestCase):
    def test_valid_interval_accepted(self):
        validate_interval(0, 1)
        validate_interval(-5, 5)

    def test_zero_length_rejected(self):
        with self.assertRaises(ValueError):
            validate_interval(3, 3)

    def test_reversed_endpoints_rejected(self):
        with self.assertRaises(ValueError):
            validate_interval(5, 2)

    def test_none_endpoint_rejected(self):
        with self.assertRaises(ValueError):
            validate_interval(None, 2)
        with self.assertRaises(ValueError):
            validate_interval(1, None)


class TestMergeIntervals(unittest.TestCase):
    def test_sorted_disjoint(self):
        self.assertEqual(merge_intervals([(1, 2), (3, 4)]), [(1, 2), (3, 4)])

    def test_adjacent_half_open_intervals_glue_together(self):
        # [1,3) 与 [3,5) 之间没有空隙，必须粘合
        self.assertEqual(merge_intervals([(1, 3), (3, 5)]), [(1, 5)])

    def test_overlapping_merged(self):
        self.assertEqual(merge_intervals([(1, 4), (3, 6)]), [(1, 6)])

    def test_unsorted_input(self):
        self.assertEqual(merge_intervals([(7, 9), (1, 3), (2, 5)]), [(1, 5), (7, 9)])

    def test_contained_interval(self):
        self.assertEqual(merge_intervals([(1, 10), (3, 4)]), [(1, 10)])

    def test_empty(self):
        self.assertEqual(merge_intervals([]), [])


class TestUncoveredParts(unittest.TestCase):
    def test_fully_covered_single(self):
        self.assertEqual(uncovered_parts((0, 10), [(0, 10)]), [])

    def test_fully_covered_by_adjacent_segments(self):
        # 相邻小段的并集覆盖整段
        self.assertEqual(uncovered_parts((0, 10), [(0, 4), (4, 7), (7, 10)]), [])

    def test_no_cover_at_all(self):
        self.assertEqual(uncovered_parts((0, 10), []), [(0, 10)])

    def test_cover_does_not_exist_for_key_returns_whole_interval(self):
        self.assertEqual(uncovered_parts((2, 8), [(20, 30)]), [(2, 8)])

    def test_gap_in_the_middle(self):
        self.assertEqual(
            uncovered_parts((0, 10), [(0, 3), (5, 10)]),
            [(3, 5)],
        )

    def test_cover_starts_late(self):
        self.assertEqual(uncovered_parts((0, 10), [(4, 10)]), [(0, 4)])

    def test_cover_ends_early(self):
        self.assertEqual(uncovered_parts((0, 10), [(0, 6)]), [(6, 10)])

    def test_cover_extends_beyond_target(self):
        self.assertEqual(uncovered_parts((3, 7), [(0, 100)]), [])

    def test_multiple_gaps_with_overlapping_unsorted_cover(self):
        cover = [(9, 12), (1, 3), (2, 4)]
        self.assertEqual(
            uncovered_parts((0, 12), cover),
            [(0, 1), (4, 9)],
        )

    def test_half_open_boundary_point_is_covered(self):
        # 子区间 [0,5)，父区间 [0,5) 完全覆盖；端点 5 不属于任何区间
        self.assertEqual(uncovered_parts((0, 5), [(0, 5)]), [])
        # 父区间只到 4，则 [4,5) 缺覆盖
        self.assertEqual(uncovered_parts((0, 5), [(0, 4)]), [(4, 5)])


if __name__ == "__main__":
    unittest.main()
