"""区间工具函数的单元测试。"""

import unittest

from transit_audit import (
    ByteRange,
    excess_ranges,
    intersect_ranges,
    merge_ranges,
    uncovered_ranges,
)


class MergeRangesTest(unittest.TestCase):
    def test_empty_input(self):
        self.assertEqual(merge_ranges([]), ())

    def test_sorts_and_merges_overlapping(self):
        result = merge_ranges([ByteRange(5, 10), ByteRange(0, 6), ByteRange(12, 15)])
        self.assertEqual(result, (ByteRange(0, 10), ByteRange(12, 15)))

    def test_adjacent_ranges_merge_without_gap(self):
        # 半开区间：[0,10) 与 [10,20) 首尾相接，视为连续。
        result = merge_ranges([ByteRange(0, 10), ByteRange(10, 20)])
        self.assertEqual(result, (ByteRange(0, 20),))

    def test_nested_ranges_merge(self):
        result = merge_ranges([ByteRange(0, 100), ByteRange(10, 20), ByteRange(30, 40)])
        self.assertEqual(result, (ByteRange(0, 100),))

    def test_keeps_disjoint_ranges(self):
        result = merge_ranges([ByteRange(0, 5), ByteRange(6, 10)])
        self.assertEqual(result, (ByteRange(0, 5), ByteRange(6, 10)))


class IntersectRangesTest(unittest.TestCase):
    def test_clips_partial_overlap(self):
        result = intersect_ranges([ByteRange(0, 50), ByteRange(60, 120)], ByteRange(20, 100))
        self.assertEqual(result, (ByteRange(20, 50), ByteRange(60, 100)))

    def test_drops_ranges_fully_outside(self):
        result = intersect_ranges([ByteRange(0, 10), ByteRange(100, 120)], ByteRange(20, 80))
        self.assertEqual(result, ())

    def test_keeps_contained_ranges(self):
        result = intersect_ranges([ByteRange(30, 40), ByteRange(50, 60)], ByteRange(0, 100))
        self.assertEqual(result, (ByteRange(30, 40), ByteRange(50, 60)))


class UncoveredRangesTest(unittest.TestCase):
    def test_fully_covered_including_adjacent(self):
        covered = (ByteRange(0, 30), ByteRange(30, 60), ByteRange(60, 100))
        self.assertEqual(uncovered_ranges(covered, ByteRange(0, 100)), ())

    def test_gap_in_the_middle(self):
        covered = (ByteRange(0, 40), ByteRange(60, 100))
        self.assertEqual(
            uncovered_ranges(covered, ByteRange(0, 100)),
            (ByteRange(40, 60),),
        )

    def test_gaps_at_both_ends(self):
        self.assertEqual(
            uncovered_ranges((ByteRange(30, 70),), ByteRange(0, 100)),
            (ByteRange(0, 30), ByteRange(70, 100)),
        )

    def test_nothing_covered(self):
        self.assertEqual(
            uncovered_ranges((), ByteRange(10, 20)),
            (ByteRange(10, 20),),
        )

    def test_overlapping_pieces_still_cover(self):
        covered = (ByteRange(0, 55), ByteRange(45, 100))
        self.assertEqual(uncovered_ranges(covered, ByteRange(0, 100)), ())


class ExcessRangesTest(unittest.TestCase):
    def test_no_excess_when_inside(self):
        self.assertEqual(excess_ranges((ByteRange(10, 90),), ByteRange(0, 100)), ())

    def test_excess_on_both_sides(self):
        result = excess_ranges((ByteRange(0, 120),), ByteRange(20, 100))
        self.assertEqual(result, (ByteRange(0, 20), ByteRange(100, 120)))

    def test_disjoint_excess_pieces_merged(self):
        result = excess_ranges(
            (ByteRange(0, 10), ByteRange(5, 15), ByteRange(110, 130)),
            ByteRange(20, 100),
        )
        self.assertEqual(result, (ByteRange(0, 15), ByteRange(110, 130)))

    def test_touching_boundary_is_not_excess(self):
        # 半开区间贴边（越界部分的起点恰好落在边界上）不产生额外越界。
        self.assertEqual(excess_ranges((ByteRange(20, 100),), ByteRange(20, 100)), ())
        self.assertEqual(excess_ranges((ByteRange(0, 100),), ByteRange(20, 100)), (ByteRange(0, 20),))
        self.assertEqual(excess_ranges((ByteRange(20, 100),), ByteRange(20, 80)), (ByteRange(80, 100),))

    def test_fully_disjoint_range_is_all_excess(self):
        # 与边界完全不相交的区间整体属于越界部分。
        self.assertEqual(
            excess_ranges((ByteRange(0, 20),), ByteRange(20, 100)),
            (ByteRange(0, 20),),
        )


if __name__ == "__main__":
    unittest.main()
