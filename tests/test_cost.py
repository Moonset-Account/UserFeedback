"""Cost 格运算与递归实例数公式的边界测试。"""

import sys
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from budget_guard.model import (  # noqa: E402
    Cost,
    bound_add,
    bound_max,
    bound_scale,
    cost_add,
    cost_max,
    cost_scale,
)
from budget_guard.analyzer import instance_count  # noqa: E402


class TestBoundArithmetic(unittest.TestCase):
    def test_add_propagates_unknown(self):
        self.assertEqual(bound_add(1, 2), 3)
        self.assertIsNone(bound_add(None, 2))
        self.assertIsNone(bound_add(2, None))
        self.assertIsNone(bound_add(None, None))

    def test_max_takes_max_not_sum(self):
        self.assertEqual(bound_max(3, 7), 7)
        self.assertEqual(bound_max(7, 3), 7)
        # 互斥臂中任一臂无界 -> max 无有限上界
        self.assertIsNone(bound_max(None, 7))
        self.assertIsNone(bound_max(7, None))

    def test_scale_zero_boundary(self):
        self.assertEqual(bound_scale(5, 3), 15)
        # 可证明零次：即使单位成本无界，总量也是 0
        self.assertEqual(bound_scale(None, 0), 0)
        self.assertEqual(bound_scale(123456, 0), 0)
        # 单位成本为 0：即使次数无证据，该维度仍为 0
        self.assertEqual(bound_scale(0, None), 0)
        # 次数无证据且单位成本为正 -> 无界
        self.assertIsNone(bound_scale(5, None))
        self.assertIsNone(bound_scale(None, 3))
        self.assertIsNone(bound_scale(None, None))

    def test_cost_helpers(self):
        a = Cost(3, 10)
        b = Cost(5, 2)
        self.assertEqual(cost_add(a, b), Cost(8, 12))
        self.assertEqual(cost_max(a, b), Cost(5, 10))
        self.assertEqual(cost_scale(Cost(4, 7), 3), Cost(12, 21))
        self.assertEqual(cost_scale(Cost(None, 2), 0), Cost(0, 0))
        # 维度独立：一个维度可证明为 0，另一个无界
        self.assertEqual(cost_scale(Cost(0, 2), None), Cost(0, None))

    def test_cost_rejects_negative_and_bool(self):
        with self.assertRaises(ValueError):
            Cost(-1, 0)
        with self.assertRaises(ValueError):
            Cost(0, True)  # bool 不是合法整数上界


class TestInstanceCount(unittest.TestCase):
    def test_unknown_depth_is_unbounded(self):
        self.assertIsNone(instance_count(None, 1))
        self.assertIsNone(instance_count(None, 2))

    def test_zero_depth_is_zero(self):
        self.assertEqual(instance_count(0, 2), 0)
        self.assertEqual(instance_count(0, None), 0)

    def test_depth_one_always_single_instance(self):
        # 边界：只有一层时，未知扇出无关
        self.assertEqual(instance_count(1, None), 1)
        self.assertEqual(instance_count(1, 99), 1)

    def test_unknown_fanout_beyond_first_level_unbounded(self):
        self.assertIsNone(instance_count(2, None))
        self.assertIsNone(instance_count(9, None))

    def test_linear_fanout_one(self):
        self.assertEqual(instance_count(1, 1), 1)
        self.assertEqual(instance_count(3, 1), 3)
        self.assertEqual(instance_count(10, 1), 10)

    def test_geometric_fanout(self):
        # depth=4, f=2 -> 1+2+4+8 = 15
        self.assertEqual(instance_count(4, 2), 15)
        # depth=3, f=3 -> 1+3+9 = 13
        self.assertEqual(instance_count(3, 3), 13)
        # f=0 时只有根实例
        self.assertEqual(instance_count(5, 0), 1)
        # 大指数使用闭式整数运算，不逐层循环
        self.assertEqual(instance_count(2, 10**6), 10**6 + 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
