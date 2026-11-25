"""题面要求留出的三个可复算例子。

每个测试都写清楚输入、期望输出和手算过程；数值全部是小整数，
可以照着断言逐行复算。
"""

import unittest

from grouping_sets import GroupingSetsEngine, GroupingSetsSpec
from tests.support import row_at


def example_spec():
    return GroupingSetsSpec(
        grouping_columns=["grp"],
        grouping_sets=[["grp"], []],
        aggregates=[
            ("count", "*", "n"),
            ("count", "v", "n_v"),
            ("sum", "v", "total"),
            ("min", "v", "lowest"),
        ],
    )


class RepeatedMinimumExample(unittest.TestCase):
    """例子一：重复最小值。

    数据（组 g）：v = [4, 1, 1, 7]
        第一行: n=4, n_v=4, sum=4+1+1+7=13, min=min(4,1,1,7)=1
    撤回一个 v=1 后（值 1 还出现过一次，最小值必须仍是 1）：
        [4, 1, 7] -> n=3, sum=12, min=1
    再撤回另一个 v=1 后（最小值上移到 4；只靠"记住一个最小值"会错）：
        [4, 7]    -> n=2, sum=11, min=4
    """

    def test_repeated_minimum_survives_each_retraction(self):
        engine = GroupingSetsEngine(example_spec())
        engine.apply(
            adds=[
                {"grp": "g", "v": 4},
                {"grp": "g", "v": 1},
                {"grp": "g", "v": 1},
                {"grp": "g", "v": 7},
            ]
        )
        row = row_at(engine.current_snapshot(), 0, ("g",))
        self.assertEqual(row.aggregates, (4, 4, 13, 1))

        engine.apply(retracts=[{"grp": "g", "v": 1}])
        row = row_at(engine.current_snapshot(), 0, ("g",))
        self.assertEqual(row.aggregates, (3, 3, 12, 1))

        result = engine.apply(retracts=[{"grp": "g", "v": 1}])
        row = row_at(result.snapshot, 0, ("g",))
        self.assertEqual(row.aggregates, (2, 2, 11, 4))
        # 小计与总计同步变化
        self.assertEqual(row_at(result.snapshot, 1, (None,)).aggregates, (2, 2, 11, 4))

    def test_repeated_minimum_reported_change_per_batch(self):
        engine = GroupingSetsEngine(example_spec())
        engine.apply(
            adds=[{"grp": "g", "v": 1}, {"grp": "g", "v": 1}, {"grp": "g", "v": 4}]
        )
        first = engine.apply(retracts=[{"grp": "g", "v": 1}])
        # min 未变，但 count/sum 变了：叶子与小计都要 upsert，而不是 delete+insert
        leaf = [r for r in first.upserts if r.set_index == 0][0]
        self.assertEqual(leaf.aggregates, (2, 2, 5, 1))
        self.assertEqual(first.deletes, ())

        second = engine.apply(retracts=[{"grp": "g", "v": 1}])
        leaf = [r for r in second.upserts if r.set_index == 0][0]
        self.assertEqual(leaf.aggregates, (1, 1, 4, 4))


class AllNullAggregateColumnExample(unittest.TestCase):
    """例子二：全空聚合列。

    数据（组 g）：v = [None, None, None]
        n    = COUNT(*)          = 3
        n_v  = COUNT(v)          = 0     （NULL 不计）
        total= SUM(v)            = None  （没有非空值）
        lowest=MIN(v)            = None
    再加一行 v=8：
        n=4, n_v=1, sum=8, min=8
    撤掉它回到全空：
        n=3, n_v=0, sum=None, min=None
    """

    def test_all_null_column_yields_null_sum_and_min_but_nonzero_count(self):
        engine = GroupingSetsEngine(example_spec())
        engine.apply(
            adds=[
                {"grp": "g", "v": None},
                {"grp": "g", "v": None},
                {"grp": "g", "v": None},
            ]
        )
        row = row_at(engine.current_snapshot(), 0, ("g",))
        self.assertEqual(row.aggregates, (3, 0, None, None))

    def test_null_column_transitions_back_and_forth(self):
        engine = GroupingSetsEngine(example_spec())
        engine.apply(adds=[{"grp": "g", "v": None}])
        engine.apply(adds=[{"grp": "g", "v": 8}])
        self.assertEqual(
            row_at(engine.current_snapshot(), 0, ("g",)).aggregates, (2, 1, 8, 8)
        )
        result = engine.apply(retracts=[{"grp": "g", "v": 8}])
        self.assertEqual(
            row_at(result.snapshot, 0, ("g",)).aggregates, (1, 0, None, None)
        )
        # SUM/MIN 从 8 变回 None 属于内容变化，必须出现在 upserts 里
        self.assertIn((0, ("g",)), {(r.set_index, r.grouping_values) for r in result.upserts})

    def test_empty_grouping_set_with_no_data_is_zero_and_null(self):
        engine = GroupingSetsEngine(example_spec())
        grand = row_at(engine.current_snapshot(), 1, (None,))
        self.assertEqual(grand.aggregates, (0, 0, None, None))

    def test_all_null_column_on_empty_grouping_set_after_last_retraction(self):
        engine = GroupingSetsEngine(example_spec())
        engine.apply(adds=[{"grp": "g", "v": None}])
        result = engine.apply(retracts=[{"grp": "g", "v": None}])
        self.assertEqual(
            row_at(result.snapshot, 1, (None,)).aggregates, (0, 0, None, None)
        )
        self.assertIsNone(row_at(result.snapshot, 0, ("g",)))


class NegativeNumberExample(unittest.TestCase):
    """例子三：负数。

    数据（组 g）：v = [-5, -2, -10]
        n=3, n_v=3, sum=-5 + (-2) + (-10) = -17, min=-10
    撤回 v=-10：
        [-5, -2] -> n=2, sum=-7, min=-5
    再加回 v=-3：
        [-5, -2, -3] -> n=3, sum=-10, min=-5
    """

    def test_negative_values_sum_and_min(self):
        engine = GroupingSetsEngine(example_spec())
        engine.apply(
            adds=[
                {"grp": "g", "v": -5},
                {"grp": "g", "v": -2},
                {"grp": "g", "v": -10},
            ]
        )
        row = row_at(engine.current_snapshot(), 0, ("g",))
        self.assertEqual(row.aggregates, (3, 3, -17, -10))

        engine.apply(retracts=[{"grp": "g", "v": -10}])
        row = row_at(engine.current_snapshot(), 0, ("g",))
        self.assertEqual(row.aggregates, (2, 2, -7, -5))

        engine.apply(adds=[{"grp": "g", "v": -3}])
        row = row_at(engine.current_snapshot(), 0, ("g",))
        self.assertEqual(row.aggregates, (3, 3, -10, -5))

    def test_min_of_mixed_sign_values(self):
        engine = GroupingSetsEngine(example_spec())
        engine.apply(
            adds=[{"grp": "g", "v": -1}, {"grp": "g", "v": 0}, {"grp": "g", "v": 3}]
        )
        row = row_at(engine.current_snapshot(), 0, ("g",))
        self.assertEqual(row.aggregates, (3, 3, 2, -1))

    def test_negative_floats_use_exact_summation(self):
        spec = GroupingSetsSpec(
            ["g"], [["g"]], aggregates=[("sum", "v", "s"), ("min", "v", "m")]
        )
        engine = GroupingSetsEngine(spec)
        engine.apply(
            adds=[
                {"g": 1, "v": -0.1},
                {"g": 1, "v": -0.2},
            ]
        )
        row = row_at(engine.current_snapshot(), 0, (1,))
        self.assertAlmostEqual(row.aggregates[0], -0.30000000000000004, places=15)
        self.assertEqual(row.aggregates[1], -0.2)

    def test_negatives_cancel_to_zero_but_group_still_exists(self):
        engine = GroupingSetsEngine(example_spec())
        engine.apply(adds=[{"grp": "g", "v": -5}, {"grp": "g", "v": 5}])
        row = row_at(engine.current_snapshot(), 0, ("g",))
        self.assertEqual(row.aggregates, (2, 2, 0, -5))
        self.assertIsNotNone(row)


if __name__ == "__main__":
    unittest.main()
