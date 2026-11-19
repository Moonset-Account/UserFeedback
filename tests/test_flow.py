"""运输可行性判定（publication.flow.feasible）的直接单元测试。

这些用例不经过求解器，直接验证最大流封装的边界语义，包括固定值、
上下界裁剪、行列总量不匹配与容量耗尽等场景。
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from publication.flow import feasible, _Dinic  # noqa: E402


class DinicBasicTest(unittest.TestCase):
    def test_simple_max_flow(self) -> None:
        net = _Dinic(4)
        net.add_edge(0, 1, 3)
        net.add_edge(0, 2, 2)
        net.add_edge(1, 3, 2)
        net.add_edge(2, 3, 3)
        self.assertEqual(net.max_flow(0, 3), 4)

    def test_zero_capacity_edges(self) -> None:
        net = _Dinic(2)
        net.add_edge(0, 1, 0)
        self.assertEqual(net.max_flow(0, 1), 0)

    def test_negative_capacity_rejected(self) -> None:
        net = _Dinic(2)
        with self.assertRaises(ValueError):
            net.add_edge(0, 1, -1)


class FeasibleFixedCellsTest(unittest.TestCase):
    ROW_SUMS = (5, 9)
    COL_SUMS = (7, 7)
    UPPER = [[9, 9], [9, 9]]

    def test_true_table_is_feasible(self) -> None:
        # 真实表 [[1,4],[6,3]] 固定三格后，隐藏格仍可完成。
        self.assertTrue(feasible(
            2, 2, self.ROW_SUMS, self.COL_SUMS, self.UPPER,
            fixed=((0, 1, 4), (1, 0, 6), (1, 1, 3)),
        ))

    def test_wrong_fixed_value_blocks(self) -> None:
        # 把可见格固定为与真实值不符的 5，运输问题无解。
        self.assertFalse(feasible(
            2, 2, self.ROW_SUMS, self.COL_SUMS, self.UPPER,
            fixed=((0, 1, 5), (1, 0, 6), (1, 1, 3)),
        ))

    def test_fixed_value_above_upper_blocks(self) -> None:
        self.assertFalse(feasible(
            2, 2, self.ROW_SUMS, self.COL_SUMS, self.UPPER,
            fixed=((0, 1, 9), (1, 0, 6), (1, 1, 3)),
            clipped_upper=((0, 1, 4),),
        ))

    def test_row_col_totals_mismatch_blocks(self) -> None:
        self.assertFalse(feasible(
            2, 2, (5, 9), (7, 8), self.UPPER,
        ))


class FeasibleClipTest(unittest.TestCase):
    ROW_SUMS = (3, 3)
    COL_SUMS = (3, 3)
    UPPER = [[3, 9], [9, 9]]

    def test_lower_bound_feasible_then_infeasible(self) -> None:
        # 真实表 [[2,1],[1,2]]，全隐藏；要求 (0,0)>=3 可行，>=4 不可行。
        self.assertTrue(feasible(
            2, 2, self.ROW_SUMS, self.COL_SUMS, self.UPPER,
            clipped_lower=((0, 0, 3),),
        ))
        self.assertFalse(feasible(
            2, 2, self.ROW_SUMS, self.COL_SUMS, self.UPPER,
            clipped_lower=((0, 0, 4),),
        ))

    def test_upper_bound_feasible_then_infeasible(self) -> None:
        # 要求 (0,0)<=0 可行（存在全零角点表）；与下界冲突时不可行。
        self.assertTrue(feasible(
            2, 2, self.ROW_SUMS, self.COL_SUMS, self.UPPER,
            clipped_upper=((0, 0, 0),),
        ))
        self.assertFalse(feasible(
            2, 2, self.ROW_SUMS, self.COL_SUMS, self.UPPER,
            clipped_lower=((0, 0, 2),),
            clipped_upper=((0, 0, 1),),
        ))

    def test_capacity_exhaustion_blocks(self) -> None:
        # 行 0 总量 3，却要求 (0,0)>=4（上界仅 3）：预扣即失败。
        self.assertFalse(feasible(
            2, 2, self.ROW_SUMS, self.COL_SUMS, self.UPPER,
            clipped_lower=((0, 0, 4),),
        ))

    def test_zero_total_network(self) -> None:
        # 全零表：需求为 0 时无需任何流即可行。
        upper = [[9, 9], [9, 9]]
        self.assertTrue(feasible(2, 2, (0, 0), (0, 0), upper))
        self.assertFalse(feasible(
            2, 2, (0, 0), (0, 0), upper,
            clipped_lower=((0, 0, 1),),
        ))


if __name__ == "__main__":
    unittest.main(verbosity=2)
