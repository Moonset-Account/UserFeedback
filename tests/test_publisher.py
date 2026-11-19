"""发布模块验收测试（unittest）。

覆盖维度
========
* 可接受结果：初始即模糊（零额外成本）、成本+字典序择优、上界裁剪；
* 应被阻止/拒绝的结果：补隐藏无法解锁时 ``feasible=False`` 且报告仍可
  推出的值、非法输入一律 ``ValueError``；
* 边界/交错：单格表、零成本候选、无候选、同成本并列、必须全隐藏、
  行列合计始终保留；
* 交叉约束：对小规模夹具用独立暴力枚举器（tests/_brute.py）逐子集
  复核最小值/最大值与最优方案，与最大流实现相互独立。
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stdout

# 兼容 ``python -I`` 隔离运行：显式把工作区根目录加入模块搜索路径。
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from publication import plan_release
from publication.cli import main as cli_main

from tests._brute import brute_best_subset, brute_ranges


class ForcedThenFullHideTest(unittest.TestCase):
    """2x2：初始唯一推出，单/双隐藏均被阻止，只有全隐藏可行。"""

    TABLE = [[1, 4], [6, 3]]
    UPPER = 9
    SENSITIVE = ((0, 0),)
    CANDIDATES = ((0, 1), (1, 0), (1, 1))
    COSTS = {(0, 1): 4, (1, 0): 4, (1, 1): 4}

    def setUp(self) -> None:
        self.plan = plan_release(
            self.TABLE, self.UPPER, self.SENSITIVE,
            self.CANDIDATES, self.COSTS,
        )

    def test_initial_value_is_forced(self) -> None:
        # 可接受结果之一：观察者在初始发布下唯一推出 s=1。
        self.assertEqual(self.plan.initial_ranges, {(0, 0): (1, 1)})
        self.assertEqual(self.plan.initial_forced, {(0, 0): 1})
        self.assertFalse(
            self.plan.feasible and self.plan.cost == 0,
            "初始状态下敏感格不应被视为模糊",
        )

    def test_all_partial_subsets_blocked_full_hide_required(self) -> None:
        # 应被阻止的结果：任何真子集都解锁不了，只有全隐藏可行。
        self.assertTrue(self.plan.feasible)
        self.assertEqual(set(self.plan.extra_hidden), set(self.CANDIDATES))
        self.assertEqual(self.plan.cost, 12)
        # 全隐藏后 s 可在 [0,5] 内取值（受行合计 5、列合计 7 约束）。
        self.assertEqual(self.plan.final_ranges, {(0, 0): (0, 5)})
        self.assertEqual(self.plan.still_forced, {})

    def test_margins_always_published(self) -> None:
        # 交叉约束：行、列合计在任何方案中都必须保留。
        self.assertEqual(self.plan.row_sums, (5, 9))
        self.assertEqual(self.plan.col_sums, (7, 7))

    def test_cross_check_brute_force(self) -> None:
        init = brute_ranges(
            self.TABLE,
            [[self.UPPER] * 2 for _ in range(2)],
            frozenset(self.SENSITIVE),
            self.SENSITIVE,
        )
        self.assertEqual(init, self.plan.initial_ranges)

        subset, cost, ranges, full_ranges = brute_best_subset(
            self.TABLE,
            [[self.UPPER] * 2 for _ in range(2)],
            self.SENSITIVE,
            self.CANDIDATES,
            self.COSTS,
        )
        self.assertEqual(subset, self.plan.extra_hidden)
        self.assertEqual(cost, self.plan.cost)
        self.assertEqual(ranges, self.plan.final_ranges)
        self.assertEqual(full_ranges, self.plan.full_hide_ranges)


class CostAndLexicographicTieTest(unittest.TestCase):
    """3x3：两个等成本四元环方案，取坐标序列字典序最小者。"""

    TABLE = [
        [3, 1, 2],
        [2, 1, 1],
        [1, 2, 2],
    ]
    UPPER = 9
    SENSITIVE = ((0, 0),)
    # 环 1：(0,1),(1,0),(1,1)；环 2：(0,2),(2,0),(2,2)
    CANDIDATES = ((0, 1), (0, 2), (1, 0), (1, 1), (2, 0), (2, 2))
    COSTS = {
        (0, 1): 2, (1, 0): 2, (1, 1): 1,   # 环 1 合计 5
        (0, 2): 3, (2, 0): 1, (2, 2): 1,   # 环 2 合计 5
    }

    def setUp(self) -> None:
        self.plan = plan_release(
            self.TABLE, self.UPPER, self.SENSITIVE,
            self.CANDIDATES, self.COSTS,
        )

    def test_initial_forced_and_tie_breaking(self) -> None:
        self.assertEqual(self.plan.initial_ranges, {(0, 0): (3, 3)})
        self.assertEqual(self.plan.initial_forced, {(0, 0): 3})
        self.assertTrue(self.plan.feasible)
        self.assertEqual(self.plan.cost, 5)
        # 两个成本 5 的方案：
        #   G1 = ((0,1),(1,0),(1,1))
        #   G2 = ((0,2),(2,0),(2,2))
        # 字典序：(0,1) < (0,2)，故取 G1。
        self.assertEqual(
            self.plan.extra_hidden,
            ((0, 1), (1, 0), (1, 1)),
        )
        low, high = self.plan.final_ranges[(0, 0)]
        self.assertLess(low, high)
        self.assertEqual((low, high), (2, 4))

    def test_lexicographic_key_rule(self) -> None:
        # 明确交错规则：等成本时按完整有序坐标序列比较，而非按集合大小。
        g1 = ((0, 1), (1, 0), (1, 1))
        g2 = ((0, 2), (2, 0), (2, 2))
        self.assertEqual(min((5, g1), (5, g2)), (5, g1))

    def test_input_order_does_not_change_result(self) -> None:
        # 候选与成本以乱序传入，结果必须仍是行优先有序的同一最优方案。
        shuffled_candidates = (
            (2, 2), (0, 2), (1, 1), (2, 0), (1, 0), (0, 1),
        )
        shuffled_costs = {p: self.COSTS[p] for p in shuffled_candidates}
        plan = plan_release(
            self.TABLE, self.UPPER,
            sensitive=((0, 0),),
            candidates=shuffled_candidates,
            costs=shuffled_costs,
        )
        self.assertTrue(plan.feasible)
        self.assertEqual(plan.cost, 5)
        self.assertEqual(
            plan.extra_hidden,
            ((0, 1), (1, 0), (1, 1)),
        )
        self.assertEqual(
            list(plan.extra_hidden),
            sorted(plan.extra_hidden),
        )

    def test_cross_check_brute_force(self) -> None:
        upper = [[self.UPPER] * 3 for _ in range(3)]
        subset, cost, ranges, full_ranges = brute_best_subset(
            self.TABLE, upper, self.SENSITIVE,
            self.CANDIDATES, self.COSTS,
        )
        self.assertEqual(subset, self.plan.extra_hidden)
        self.assertEqual(cost, 5)
        self.assertEqual(ranges, self.plan.final_ranges)
        init = brute_ranges(
            self.TABLE, upper, frozenset(self.SENSITIVE), self.SENSITIVE
        )
        self.assertEqual(init, self.plan.initial_ranges)
        self.assertEqual(full_ranges, self.plan.full_hide_ranges)


class AlreadyAmbiguousZeroCostTest(unittest.TestCase):
    """边界：候选成本为 0 时方案成本为 0，且字典序取最小集合。

    2x2 表中 (0,0) 与对角格 (1,1) 同为必须隐藏的敏感格；初始发布下二者
    仍被可见的 (0,1)/(1,0) 唯一锁定。把这两个零成本候选隐藏后形成四元环，
    两个敏感格同时获得多种取值，总成本 0。
    """

    TABLE = [[1, 2], [3, 4]]
    UPPER = 9
    SENSITIVE = ((0, 0), (1, 1))
    CANDIDATES = ((0, 1), (1, 0))
    COSTS = {(0, 1): 0, (1, 0): 0}

    def test_zero_cost_unlocks_both_sensitive_cells(self) -> None:
        plan = plan_release(
            self.TABLE, self.UPPER, self.SENSITIVE,
            self.CANDIDATES, self.COSTS,
        )
        # 初始两个敏感格都被唯一推出。
        self.assertEqual(plan.initial_ranges,
                         {(0, 0): (1, 1), (1, 1): (4, 4)})
        self.assertEqual(plan.initial_forced, {(0, 0): 1, (1, 1): 4})
        self.assertTrue(plan.feasible)
        self.assertEqual(plan.extra_hidden, ((0, 1), (1, 0)))
        self.assertEqual(plan.cost, 0)
        for cell in self.SENSITIVE:
            low, high = plan.final_ranges[cell]
            self.assertLess(low, high)
        self.assertEqual(plan.final_ranges,
                         {(0, 0): (0, 3), (1, 1): (3, 6)})

    def test_cross_check_brute_force(self) -> None:
        upper = [[self.UPPER] * 2 for _ in range(2)]
        subset, cost, ranges, _ = brute_best_subset(
            self.TABLE, upper, self.SENSITIVE,
            self.CANDIDATES, self.COSTS,
        )
        plan = plan_release(
            self.TABLE, self.UPPER, self.SENSITIVE,
            self.CANDIDATES, self.COSTS,
        )
        self.assertEqual(subset, plan.extra_hidden)
        self.assertEqual(cost, 0)
        self.assertEqual(ranges, plan.final_ranges)
        init = brute_ranges(
            self.TABLE, upper, frozenset(self.SENSITIVE), self.SENSITIVE
        )
        self.assertEqual(init, plan.initial_ranges)


class InitiallyAmbiguousEmptyPlanTest(unittest.TestCase):
    """边界：仅必须隐藏就已模糊时，最优方案为空集、成本 0。

    2x3 表的前 2x2 四格全部为敏感格，构成带松弛的四元环：初始发布下
    每个敏感格都有多种可行取值。第三列两个候选格成本为 0，但空坐标序列
    在字典序上严格最小，最优方案不应隐藏任何候选。
    """

    TABLE = [
        [1, 2, 3],
        [3, 4, 5],
    ]
    UPPER = 9
    SENSITIVE = ((0, 0), (0, 1), (1, 0), (1, 1))
    CANDIDATES = ((0, 2), (1, 2))
    COSTS = {(0, 2): 0, (1, 2): 0}
    EXPECTED_RANGES = {
        (0, 0): (0, 3), (0, 1): (0, 3),
        (1, 0): (1, 4), (1, 1): (3, 6),
    }

    def test_empty_subset_wins(self) -> None:
        plan = plan_release(
            self.TABLE, self.UPPER, self.SENSITIVE,
            self.CANDIDATES, self.COSTS,
        )
        self.assertTrue(plan.feasible)
        self.assertEqual(plan.extra_hidden, ())
        self.assertEqual(plan.cost, 0)
        self.assertEqual(plan.initial_forced, {})
        self.assertEqual(plan.initial_ranges, self.EXPECTED_RANGES)
        self.assertEqual(plan.final_ranges, plan.initial_ranges)
        for cell in self.SENSITIVE:
            low, high = plan.initial_ranges[cell]
            self.assertLess(low, high)

    def test_cross_check_brute_force(self) -> None:
        upper = [[self.UPPER] * 3 for _ in range(2)]
        subset, cost, ranges, _ = brute_best_subset(
            self.TABLE, upper, self.SENSITIVE,
            self.CANDIDATES, self.COSTS,
        )
        plan = plan_release(
            self.TABLE, self.UPPER, self.SENSITIVE,
            self.CANDIDATES, self.COSTS,
        )
        self.assertEqual(subset, ())
        self.assertEqual(cost, 0)
        self.assertEqual(ranges, self.EXPECTED_RANGES)
        init = brute_ranges(
            self.TABLE, upper, frozenset(self.SENSITIVE), self.SENSITIVE
        )
        self.assertEqual(init, plan.initial_ranges)


class UpperBoundClippingTest(unittest.TestCase):
    """边界：上界 9 裁剪观察者区间，且逐格上界矩阵被尊重。"""

    TABLE = [[2, 1], [1, 2]]
    UPPER_MATRIX = [
        [3, 9],
        [9, 9],
    ]
    SENSITIVE = ((0, 0),)
    CANDIDATES = ((0, 1), (1, 0), (1, 1))
    COSTS = {(0, 1): 5, (1, 0): 5, (1, 1): 5}

    def test_initial_forced_and_clipped_full_range(self) -> None:
        plan = plan_release(
            self.TABLE, self.UPPER_MATRIX, self.SENSITIVE,
            self.CANDIDATES, self.COSTS,
        )
        # 初始：其余三格可见，s 被唯一推出为 2。
        self.assertEqual(plan.initial_ranges, {(0, 0): (2, 2)})
        self.assertTrue(plan.feasible)
        # 单藏对角格仍被锁定，必须藏齐三格形成完整四元环。
        self.assertEqual(set(plan.extra_hidden), set(self.CANDIDATES))
        self.assertEqual(plan.cost, 15)
        # 无逐格裁剪时最大值为 5；(0,0) 的上界 3 把最大值裁到 3，
        # 最小值可降到 0（隐藏后无可见格约束行 0 的取值）。
        self.assertEqual(plan.final_ranges, {(0, 0): (0, 3)})
        self.assertEqual(plan.row_sums, (3, 3))
        self.assertEqual(plan.col_sums, (3, 3))

    def test_cross_check_brute_force(self) -> None:
        full = brute_ranges(
            self.TABLE, self.UPPER_MATRIX,
            frozenset(self.SENSITIVE) | frozenset(self.CANDIDATES),
            self.SENSITIVE,
        )
        plan = plan_release(
            self.TABLE, self.UPPER_MATRIX, self.SENSITIVE,
            self.CANDIDATES, self.COSTS,
        )
        self.assertEqual(full, plan.full_hide_ranges)
        subset, cost, ranges, _ = brute_best_subset(
            self.TABLE, self.UPPER_MATRIX, self.SENSITIVE,
            self.CANDIDATES, self.COSTS,
        )
        self.assertEqual(subset, plan.extra_hidden)
        self.assertEqual(cost, plan.cost)
        self.assertEqual(ranges, plan.final_ranges)


class ImpossibleNoCandidatesTest(unittest.TestCase):
    """应被阻止：1x3 单行中无候选可藏，值被列合计永久锁定。"""

    TABLE = [[2, 3, 4]]
    UPPER = 9
    SENSITIVE = ((0, 1),)
    CANDIDATES = ()
    COSTS = {}

    def test_reports_still_forced_value(self) -> None:
        plan = plan_release(
            self.TABLE, self.UPPER, self.SENSITIVE,
            self.CANDIDATES, self.COSTS,
        )
        self.assertFalse(plan.feasible)
        self.assertEqual(plan.extra_hidden, ())
        self.assertIsNone(plan.cost)
        self.assertEqual(plan.initial_ranges, {(0, 1): (3, 3)})
        self.assertEqual(plan.still_forced, {(0, 1): 3})
        self.assertEqual(plan.full_hide_ranges, {(0, 1): (3, 3)})
        # 合计仍必须保留。
        self.assertEqual(plan.row_sums, (9,))
        self.assertEqual(plan.col_sums, (2, 3, 4))

    def test_dict_serialization(self) -> None:
        plan = plan_release(
            self.TABLE, self.UPPER, self.SENSITIVE,
            self.CANDIDATES, self.COSTS,
        )
        payload = plan.to_dict()
        json.dumps(payload)  # 必须可 JSON 序列化
        self.assertFalse(payload["feasible"])
        self.assertEqual(payload["still_forced"], {"0,1": 3})
        self.assertIsNone(payload["cost"])


class ImpossibleFullHideTest(unittest.TestCase):
    """应被阻止：连全隐藏候选都无法解锁，指出仍可推出的值。

    3x3 表中敏感格 (0,0) 的整行、整列其余格子全为 0：无论隐藏多少格，
    行/列合计都把 (0,0) 恒锁定为 0。其余 8 格全部允许隐藏仍无解。
    """

    TABLE = [
        [0, 0, 0],
        [0, 1, 2],
        [0, 2, 1],
    ]
    UPPER = 9
    SENSITIVE = ((0, 0),)
    CANDIDATES = tuple(
        (r, c) for r in range(3) for c in range(3) if (r, c) != (0, 0)
    )

    def setUp(self) -> None:
        self.costs = {p: 1 for p in self.CANDIDATES}
        self.plan = plan_release(
            self.TABLE, self.UPPER, self.SENSITIVE,
            self.CANDIDATES, self.costs,
        )

    def test_infeasible_even_with_full_hide(self) -> None:
        self.assertFalse(self.plan.feasible)
        self.assertEqual(self.plan.still_forced, {(0, 0): 0})
        self.assertEqual(self.plan.initial_ranges, {(0, 0): (0, 0)})
        self.assertEqual(self.plan.full_hide_ranges, {(0, 0): (0, 0)})
        self.assertEqual(self.plan.extra_hidden, ())
        self.assertIsNone(self.plan.cost)
        # 行列合计保留并与原表一致。
        self.assertEqual(self.plan.row_sums, (0, 3, 3))
        self.assertEqual(self.plan.col_sums, (0, 3, 3))

    def test_cross_check_brute_force(self) -> None:
        upper = [[self.UPPER] * 3 for _ in range(3)]
        subset, cost, ranges, full_ranges = brute_best_subset(
            self.TABLE, upper, self.SENSITIVE,
            self.CANDIDATES, self.costs,
        )
        self.assertIsNone(subset)
        self.assertIsNone(cost)
        self.assertEqual(ranges, {})
        self.assertEqual(full_ranges, self.plan.full_hide_ranges)
        init = brute_ranges(
            self.TABLE, upper, frozenset(self.SENSITIVE), self.SENSITIVE
        )
        self.assertEqual(init, self.plan.initial_ranges)


class MultipleSensitiveTest(unittest.TestCase):
    """交错：两个敏感格必须同时模糊，任一仍被唯一推出即方案不合格。"""

    TABLE = [
        [1, 2, 3],
        [2, 3, 1],
        [3, 1, 2],
    ]
    UPPER = 9
    SENSITIVE = ((0, 0), (2, 2))
    CANDIDATES = ((0, 1), (1, 0), (1, 1), (1, 2), (2, 1))
    COSTS = {(0, 1): 2, (1, 0): 2, (1, 1): 1,
             (1, 2): 2, (2, 1): 2}

    def test_both_cells_must_become_ambiguous(self) -> None:
        plan = plan_release(
            self.TABLE, self.UPPER, self.SENSITIVE,
            self.CANDIDATES, self.COSTS,
        )
        self.assertTrue(plan.feasible)
        for cell in self.SENSITIVE:
            low, high = plan.final_ranges[cell]
            self.assertLess(low, high, f"{cell} 仍被唯一推出")
        # 初始状态两个格子都被锁定。
        self.assertEqual(plan.initial_forced, {(0, 0): 1, (2, 2): 2})
        # 暴力交叉验证最优性。
        upper = [[self.UPPER] * 3 for _ in range(3)]
        subset, cost, ranges, _ = brute_best_subset(
            self.TABLE, upper, self.SENSITIVE,
            self.CANDIDATES, self.COSTS,
        )
        self.assertEqual(subset, plan.extra_hidden)
        self.assertEqual(cost, plan.cost)
        self.assertEqual(ranges, plan.final_ranges)


class ValidationTest(unittest.TestCase):
    """非法输入必须被拒绝（ValueError），不产生静默错误结果。"""

    TABLE = [[1, 2], [3, 4]]
    SENSITIVE = ((0, 0),)
    CANDIDATES = ((0, 1),)
    COSTS = {(0, 1): 1}

    def _plan(self, **overrides):
        kwargs = dict(
            table=self.TABLE,
            upper_bounds=9,
            sensitive=self.SENSITIVE,
            candidates=self.CANDIDATES,
            costs=self.COSTS,
        )
        kwargs.update(overrides)
        return plan_release(**kwargs)

    def test_dimension_limit(self) -> None:
        with self.assertRaises(ValueError):
            self._plan(table=[[0] * 5 for _ in range(2)])
        with self.assertRaises(ValueError):
            self._plan(table=[[0] for _ in range(5)])

    def test_negative_and_bool_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self._plan(table=[[-1, 2], [3, 4]])
        with self.assertRaises(ValueError):
            self._plan(upper_bounds=True)
        with self.assertRaises(ValueError):
            self._plan(costs={(0, 1): True})

    def test_upper_bound_range(self) -> None:
        with self.assertRaises(ValueError):
            self._plan(upper_bounds=10)
        with self.assertRaises(ValueError):
            self._plan(upper_bounds=[[9, -1], [9, 9]])

    def test_value_exceeds_upper(self) -> None:
        with self.assertRaises(ValueError):
            self._plan(upper_bounds=[[0, 9], [9, 9]])

    def test_coordinate_rules(self) -> None:
        with self.assertRaises(ValueError):
            self._plan(sensitive=((0, 5),))
        with self.assertRaises(ValueError):
            self._plan(sensitive=((0, 0), (0, 0)))
        with self.assertRaises(ValueError):
            self._plan(candidates=((0, 0),))  # 与敏感格重叠
        with self.assertRaises(ValueError):
            self._plan(candidates=((0, 1), (0, 1)))

    def test_cost_mapping_rules(self) -> None:
        with self.assertRaises(ValueError):
            self._plan(costs={(0, 1): -1})
        with self.assertRaises(ValueError):
            self._plan(costs={(1, 1): 1})  # 键不在候选中
        with self.assertRaises(ValueError):
            self._plan(costs={})  # 缺少候选成本
        with self.assertRaises(ValueError):
            self._plan(costs=[])  # 类型错误

    def test_empty_sensitive_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self._plan(sensitive=())


class CliTest(unittest.TestCase):
    """CLI 入口执行真实计算并输出 JSON。"""

    INSTANCE = {
        "table": [[1, 4], [6, 3]],
        "upper_bounds": 9,
        "sensitive": [[0, 0]],
        "candidates": [[0, 1], [1, 0], [1, 1]],
        "costs": {"0,1": 4, "1,0": 4, "1,1": 4},
    }

    def test_cli_stdin(self) -> None:
        buf = io.StringIO()
        old_stdin = sys.stdin
        try:
            sys.stdin = io.StringIO(json.dumps(self.INSTANCE))
            with redirect_stdout(buf):
                code = cli_main([])
        finally:
            sys.stdin = old_stdin
        self.assertEqual(code, 0)
        payload = json.loads(buf.getvalue())
        self.assertTrue(payload["feasible"])
        self.assertEqual(payload["cost"], 12)
        self.assertEqual(payload["row_sums"], [5, 9])
        self.assertEqual(payload["col_sums"], [7, 7])
        self.assertEqual(payload["initial_forced"], {"0,0": 1})

    def test_cli_subprocess_file(self) -> None:
        path = os.path.join(ROOT, "tests", "_fixture_tmp.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.INSTANCE, fh)
        try:
            proc = subprocess.run(
                [sys.executable, "-I", "-c",
                 "import sys; sys.path.insert(0, %r); "
                 "from publication.cli import main; "
                 "sys.exit(main([%r]))" % (ROOT, path)],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=True,
            )
        finally:
            os.remove(path)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["feasible"])
        self.assertEqual(
            payload["extra_hidden"], [[0, 1], [1, 0], [1, 1]]
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
