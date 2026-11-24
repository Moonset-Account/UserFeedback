"""evaluate_coverage 纯函数核对的单元测试。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from temporal_fk.coverage import evaluate_coverage  # noqa: E402


class TestEvaluateCoverage(unittest.TestCase):
    def test_covered_by_union_of_multiple_parents(self):
        parents = {"k": [(0, 4), (4, 10)]}
        children = [(1, "k", (0, 10))]
        self.assertEqual(evaluate_coverage(parents, children), [])

    def test_missing_parent_key_reports_whole_interval(self):
        gaps = evaluate_coverage({}, [(7, "ghost", (1, 5))])
        self.assertEqual(len(gaps), 1)
        gap = gaps[0]
        self.assertEqual(gap.child_row_id, 7)
        self.assertEqual(gap.key, "ghost")
        self.assertEqual(gap.child_interval, (1, 5))
        self.assertEqual(list(gap.missing), [(1, 5)])

    def test_shrunk_parent_reports_exact_missing_parts(self):
        parents = {"k": [(0, 3), (7, 10)]}
        children = [(1, "k", (0, 10)), (2, "k", (3, 7))]
        gaps = evaluate_coverage(parents, children)
        by_child = {gap.child_row_id: gap for gap in gaps}
        self.assertEqual(list(by_child[1].missing), [(3, 7)])
        self.assertEqual(list(by_child[2].missing), [(3, 7)])

    def test_keys_are_checked_independently(self):
        parents = {"a": [(0, 10)], "b": []}
        children = [(1, "a", (0, 10)), (2, "b", (0, 10))]
        gaps = evaluate_coverage(parents, children)
        self.assertEqual([gap.child_row_id for gap in gaps], [2])

    def test_overlapping_parent_intervals_count_as_union(self):
        parents = {"k": [(0, 6), (4, 10)]}
        children = [(1, "k", (2, 8))]
        self.assertEqual(evaluate_coverage(parents, children), [])

    def test_no_children_always_valid(self):
        self.assertEqual(evaluate_coverage({"k": [(0, 1)]}, []), [])


if __name__ == "__main__":
    unittest.main()
