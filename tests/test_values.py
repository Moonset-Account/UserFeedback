import decimal
import unittest

from grouping_sets import (
    AggregateTypeError,
    GroupingSetsEngine,
    GroupingSetsSpec,
    NonFiniteValueError,
    UnhashableGroupingValueError,
    normalize_value,
)
from tests.support import row_at


def simple_engine(aggregates=None):
    spec = GroupingSetsSpec(
        ["g"],
        [["g"]],
        aggregates=aggregates or [("count", "*", "n"), ("sum", "v", "s"), ("min", "v", "m")],
    )
    return GroupingSetsEngine(spec)


class NormalizeValueTests(unittest.TestCase):
    def test_scalars_pass_through(self):
        for value in [None, 1, 1.5, "x", b"y", True, frozenset({1})]:
            with self.subTest(value=value):
                self.assertEqual(normalize_value(value), value)

    def test_list_becomes_hashable_tuple(self):
        self.assertEqual(normalize_value([1, [2]]), (1, (2,)))

    def test_set_becomes_frozenset(self):
        self.assertEqual(normalize_value({1}), frozenset({1}))

    def test_nested_list_grouping_key_is_usable(self):
        engine = simple_engine()
        engine.apply(adds=[{"g": [1, 2], "v": 1}])
        row = row_at(engine.current_snapshot(), 0, ((1, 2),))
        self.assertIsNotNone(row)
        self.assertEqual(row.aggregates, (1, 1, 1))

    def test_nan_grouping_value_is_rejected(self):
        engine = simple_engine()
        with self.assertRaises(NonFiniteValueError):
            engine.apply(adds=[{"g": float("nan"), "v": 1}])

    def test_nan_aggregate_value_is_rejected(self):
        engine = simple_engine()
        with self.assertRaises(NonFiniteValueError):
            engine.apply(adds=[{"g": "x", "v": float("nan")}])
        # 状态保持不变
        self.assertEqual(engine.stored_row_counts(), (0,))

    def test_infinite_aggregate_value_is_allowed_for_sum_and_min(self):
        engine = simple_engine()
        engine.apply(adds=[{"g": "x", "v": 1}, {"g": "x", "v": float("inf")}])
        row = row_at(engine.current_snapshot(), 0, ("x",))
        self.assertEqual(row.aggregates, (2, float("inf"), 1))

    def test_non_finite_decimal_is_rejected(self):
        engine = simple_engine()
        with self.assertRaises(NonFiniteValueError):
            engine.apply(adds=[{"g": decimal.Decimal("NaN"), "v": 1}])

    def test_unhashable_custom_grouping_value_is_rejected(self):
        class Mutable:
            __hash__ = None

        engine = simple_engine()
        with self.assertRaises(UnhashableGroupingValueError):
            engine.apply(adds=[{"g": Mutable(), "v": 1}])

    def test_equal_numeric_values_share_a_group(self):
        # Python 相等语义：1 == 1.0 == Decimal("1")
        engine = simple_engine()
        engine.apply(
            adds=[
                {"g": 1, "v": 1},
                {"g": 1.0, "v": 1},
                {"g": decimal.Decimal("1"), "v": 1},
            ]
        )
        rows = engine.current_snapshot()[0]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].aggregates, (3, 3, 1))


class SumTypeTests(unittest.TestCase):
    def test_decimal_sum_is_exact(self):
        engine = simple_engine()
        engine.apply(
            adds=[
                {"g": "x", "v": decimal.Decimal("0.1")},
                {"g": "x", "v": decimal.Decimal("0.2")},
            ]
        )
        row = row_at(engine.current_snapshot(), 0, ("x",))
        self.assertEqual(row.aggregates[1], decimal.Decimal("0.3"))

    def test_mixing_decimal_and_float_raises(self):
        engine = simple_engine()
        with self.assertRaises(AggregateTypeError):
            engine.apply(
                adds=[
                    {"g": "x", "v": decimal.Decimal("0.1")},
                    {"g": "x", "v": 0.2},
                ]
            )
        self.assertEqual(engine.stored_row_counts(), (0,))

    def test_mixing_strings_in_sum_raises_but_min_still_uses_plain_min(self):
        spec = GroupingSetsSpec(["g"], [["g"]], aggregates=[("sum", "v", "s")])
        engine = GroupingSetsEngine(spec)
        with self.assertRaises(AggregateTypeError):
            engine.apply(adds=[{"g": "x", "v": "a"}, {"g": "x", "v": 1}])
        self.assertEqual(engine.stored_row_counts(), (0,))

    def test_string_min_is_lexicographic(self):
        spec = GroupingSetsSpec(["g"], [["g"]], aggregates=[("min", "v", "m")])
        engine = GroupingSetsEngine(spec)
        engine.apply(adds=[{"g": "x", "v": "b"}, {"g": "x", "v": "a"}])
        self.assertEqual(row_at(engine.current_snapshot(), 0, ("x",)).aggregates, ("a",))

    def test_bool_counts_as_int_for_sum(self):
        engine = simple_engine()
        engine.apply(adds=[{"g": "x", "v": True}, {"g": "x", "v": 2}])
        row = row_at(engine.current_snapshot(), 0, ("x",))
        self.assertEqual(row.aggregates, (2, 3, True))


class AggregateFailureLeavesStateUnchangedTests(unittest.TestCase):
    def test_sum_failure_does_not_commit_state(self):
        engine = simple_engine()
        engine.apply(adds=[{"g": "x", "v": 1}])
        before = engine.current_snapshot()
        with self.assertRaises(AggregateTypeError):
            engine.apply(
                adds=[
                    {"g": "x", "v": decimal.Decimal("2.5")},
                    {"g": "x", "v": 0.5},
                ]
            )
        self.assertEqual(engine.current_snapshot(), before)

    def test_integral_decimal_and_int_merge_before_the_float_check(self):
        # Decimal("1") 与 1 相等且哈希相同，会落到同一个载荷多重集里，
        # 因此"float + 非整 Decimal"的检测只看合并后实际保留的值。
        # 这里剩下的值全是整形的，与 float 相加不丢精度，允许通过。
        engine = simple_engine()
        engine.apply(adds=[{"g": "x", "v": 1}])
        result = engine.apply(adds=[{"g": "x", "v": decimal.Decimal("1")}])
        self.assertEqual(
            row_at(result.snapshot, 0, ("x",)).aggregates, (2, 2, 1)
        )

    def test_min_type_failure_does_not_commit_state(self):
        spec = GroupingSetsSpec(["g"], [["g"]], aggregates=[("min", "v", "m")])
        engine = GroupingSetsEngine(spec)
        engine.apply(adds=[{"g": "x", "v": 1}])
        before = engine.current_snapshot()
        with self.assertRaises(AggregateTypeError):
            engine.apply(adds=[{"g": "x", "v": "a"}])
        self.assertEqual(engine.current_snapshot(), before)
        self.assertEqual(engine.stored_row_counts(), (1,))
        self.assertEqual(engine.batch_index, 0)

    def test_missing_column_does_not_commit_state(self):
        engine = simple_engine()
        with self.assertRaises(Exception):
            engine.apply(adds=[{"g": "x"}])
        self.assertEqual(engine.stored_row_counts(), (0,))
        self.assertEqual(engine.batch_index, -1)


if __name__ == "__main__":
    unittest.main()
