"""区间覆盖图纯函数单元测试。"""

import unittest

from rec import intervals


class TestIntervals(unittest.TestCase):
    def test_merge_overlapping_and_adjacent(self) -> None:
        self.assertEqual(
            intervals.merge([(0, 10), (5, 20), (20, 30), (40, 50)]),
            [(0, 30), (40, 50)],
        )

    def test_merge_sorts_and_drops_empty(self) -> None:
        self.assertEqual(
            intervals.merge([(100, 120), (0, 5), (5, 5), (3, 9)]),
            [(0, 9), (100, 120)],
        )

    def test_add_merges_into_existing(self) -> None:
        spans = [(0, 4)]
        spans = intervals.add(spans, 8, 12)
        spans = intervals.add(spans, 3, 9)  # 桥接两个区间
        self.assertEqual(spans, [(0, 12)])

    def test_gaps_full_coverage(self) -> None:
        self.assertEqual(intervals.gaps([(0, 100)], 100), [])
        self.assertIsNone(intervals.first_gap([(0, 100)], 100))

    def test_gaps_partial(self) -> None:
        spans = [(0, 10), (20, 30)]
        self.assertEqual(intervals.gaps(spans, 40), [(10, 20), (30, 40)])
        self.assertEqual(intervals.first_gap(spans, 40), (10, 20))

    def test_gaps_clamps_to_length(self) -> None:
        self.assertEqual(intervals.gaps([(0, 50)], 30), [])
        self.assertEqual(intervals.gaps([(10, 25)], 30), [(0, 10), (25, 30)])

    def test_covered_count(self) -> None:
        spans = intervals.merge([(0, 10), (5, 20), (30, 40)])
        self.assertEqual(intervals.covered_count(spans, 100), 30)
        self.assertEqual(intervals.covered_count(spans, 15), 15)

    def test_empty_inputs(self) -> None:
        self.assertEqual(intervals.merge([]), [])
        self.assertEqual(intervals.gaps([], 7), [(0, 7)])
        self.assertEqual(intervals.covered_count([], 7), 0)


if __name__ == "__main__":
    unittest.main()
