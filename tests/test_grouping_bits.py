import unittest

from grouping_sets import GroupingSetsEngine, GroupingSetsSpec
from tests.support import index_rows, row_at


class GroupingBitmapTests(unittest.TestCase):
    """小计空值 vs 数据 NULL：两者都是 None，靠位图区分。"""

    def setUp(self):
        self.spec = GroupingSetsSpec(
            grouping_columns=["region", "product"],
            grouping_sets=[["region", "product"], ["region"], []],
            aggregates=[("count", "*", "n"), ("sum", "amount", "total")],
        )
        self.engine = GroupingSetsEngine(self.spec)

    def test_omitted_columns_carry_none_with_bit_one(self):
        self.engine.apply(
            adds=[{"region": "n", "product": "a", "amount": 3}]
        )
        snap = self.engine.current_snapshot()
        leaf = snap[0][0]
        region = snap[1][0]
        grand = snap[2][0]

        self.assertEqual(leaf.grouping_values, ("n", "a"))
        self.assertEqual(leaf.grouping_bits, (0, 0))

        self.assertEqual(region.grouping_values, ("n", None))
        self.assertEqual(region.grouping_bits, (0, 1))

        self.assertEqual(grand.grouping_values, (None, None))
        self.assertEqual(grand.grouping_bits, (1, 1))

    def test_data_null_and_subtotal_null_share_none_but_differ_by_bitmap(self):
        # product 为数据 NULL 的叶子组，与按 region 小计行的 product 都是 None
        self.engine.apply(
            adds=[
                {"region": "n", "product": None, "amount": 5},
                {"region": "n", "product": "a", "amount": 2},
            ]
        )
        snap = self.engine.current_snapshot()
        leaf_rows = index_rows(snap[0])
        data_null = leaf_rows[(0, ("n", None))]
        subtotal = row_at(snap, 1, ("n", None))

        self.assertIsNone(data_null.grouping_values[1])
        self.assertIsNone(subtotal.grouping_values[1])
        self.assertEqual(data_null.grouping_bits, (0, 0))   # 数据 NULL
        self.assertEqual(subtotal.grouping_bits, (0, 1))    # 小计空值
        self.assertEqual(data_null.aggregates, (1, 5))
        self.assertEqual(subtotal.aggregates, (2, 7))

    def test_grouping_integer_is_the_bitmap_packed_big_endian(self):
        self.engine.apply(
            adds=[{"region": "n", "product": "a", "amount": 1}]
        )
        snap = self.engine.current_snapshot()
        self.assertEqual(snap[0][0].grouping, 0b00)
        self.assertEqual(snap[1][0].grouping, 0b01)
        self.assertEqual(snap[2][0].grouping, 0b11)

    def test_grouping_set_column_order_does_not_change_output_order(self):
        spec = GroupingSetsSpec(
            grouping_columns=["a", "b"],
            grouping_sets=[["b", "a"]],
            aggregates=[("count", "*", "n")],
        )
        engine = GroupingSetsEngine(spec)
        result = engine.apply(adds=[{"a": 1, "b": 2}])
        row = result.snapshot[0][0]
        # 输出按规格分组列顺序 a,b，而不是集合书写顺序 b,a
        self.assertEqual(row.grouping_values, (1, 2))
        self.assertEqual(row.grouping_bits, (0, 0))

    def test_grand_total_row_has_all_bits_set_and_none_values(self):
        result = self.engine.apply()
        grand = result.snapshot[2][0]
        self.assertEqual(grand.grouping_values, (None, None))
        self.assertEqual(grand.grouping_bits, (1, 1))
        self.assertTrue(grand.is_grand_total)

    def test_is_grand_total_is_false_for_subtotal(self):
        self.engine.apply(
            adds=[{"region": "n", "product": "a", "amount": 1}]
        )
        self.assertFalse(self.engine.current_snapshot()[1][0].is_grand_total)

    def test_empty_grouping_columns_means_single_set_with_no_groups(self):
        spec = GroupingSetsSpec(
            grouping_columns=[],
            grouping_sets=[[]],
            aggregates=[("count", "*", "n"), ("sum", "v", "s")],
        )
        engine = GroupingSetsEngine(spec)
        result = engine.apply(adds=[{"v": 1}, {"v": 2}, {"v": 3}])
        self.assertEqual(len(result.snapshot), 1)
        self.assertEqual(result.snapshot[0][0].grouping_values, ())
        self.assertEqual(result.snapshot[0][0].grouping_bits, ())
        self.assertEqual(result.snapshot[0][0].aggregates, (3, 6))

    def test_duplicate_grouping_sets_produce_two_separate_rows(self):
        spec = GroupingSetsSpec(
            grouping_columns=["g"],
            grouping_sets=[["g"], ["g"]],
            aggregates=[("count", "*", "n")],
        )
        engine = GroupingSetsEngine(spec)
        result = engine.apply(adds=[{"g": "x"}])
        values = [(row.set_index, row.grouping_values, row.grouping_bits)
                  for row in result.rows]
        self.assertEqual(
            values,
            [(0, ("x",), (0,)), (1, ("x",), (0,))],
        )
        self.assertEqual(
            [row.aggregates for row in result.rows], [(1,), (1,)]
        )


class OmittedColumnNullTests(unittest.TestCase):
    def test_null_in_an_omitted_column_is_not_a_grouping_key(self):
        # 两个 product 值在 region 小计下必须合并成一行
        spec = GroupingSetsSpec(
            grouping_columns=["region", "product"],
            grouping_sets=[["region"]],
            aggregates=[("count", "*", "n"), ("count", "product", "n_product")],
        )
        engine = GroupingSetsEngine(spec)
        result = engine.apply(
            adds=[
                {"region": "n", "product": None},
                {"region": "n", "product": "a"},
            ]
        )
        rows = result.snapshot[0]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].grouping_values, ("n", None))
        self.assertEqual(rows[0].grouping_bits, (0, 1))
        self.assertEqual(rows[0].aggregates, (2, 1))

    def test_no_sentinel_object_is_used_for_subtotal_nulls(self):
        spec = GroupingSetsSpec(
            grouping_columns=["a"],
            grouping_sets=[[], ["a"]],
            aggregates=[("count", "*", "n")],
        )
        engine = GroupingSetsEngine(spec)
        engine.apply(adds=[{"a": "x"}])
        for row in engine.current_rows():
            for value in row.grouping_values:
                self.assertTrue(value is None or isinstance(value, (str, int)))


if __name__ == "__main__":
    unittest.main()
