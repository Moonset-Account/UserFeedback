"""Tests for physical keys, measured stable sorts and fee tables."""

import unittest

from window_planning import (
    Direction,
    FeeSchedule,
    INITIAL_KEY,
    KeyRole,
    MissingFeeError,
    Nulls,
    OrderKey,
    PhysKey,
    ROWNO_COLUMN,
    TiePolicy,
    WindowFunction,
    WindowSpec,
    candidate_keys,
    can_reuse,
    load_rows,
    measured_partial_sort,
    measured_stable_sort,
)
from window_planning.physical import (
    order_part,
    partial_sort_key,
    partition_part,
    partition_prefix_match,
    rowno_part,
)


FEE = FeeSchedule(
    length_fee={1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6},
    comparison_brackets={4: 2, 8: 4, 16: 8},
)


def _spec(
    name: str = "w",
    partition_by=("dept",),
    order_by=(OrderKey("salary", Direction.ASC, Nulls.LAST),),
    tie_policy=TiePolicy.ROW_NUMBER,
    function=WindowFunction.ROW_NUMBER,
) -> WindowSpec:
    return WindowSpec(name, tuple(partition_by), tuple(order_by), tie_policy, function)


class MeasuredSortTest(unittest.TestCase):
    def test_full_sort_orders_rows_and_counts_every_comparison(self):
        rows = load_rows(
            [
                {"v": 3},
                {"v": 1},
                {"v": 2},
                {"v": 1},
            ]
        )
        key = PhysKey((order_part(OrderKey("v", Direction.ASC, Nulls.LAST)), rowno_part()))
        measurement = measured_stable_sort(rows, tuple(range(4)), key)
        values = [rows[i]["v"] for i in measurement.permutation]
        self.assertEqual(values, [1, 1, 2, 3])
        # Timsort really runs comparisons; the counter observes each one.
        self.assertGreater(measurement.comparisons, 0)
        self.assertEqual(measurement.group_sizes, (4,))

    def test_stable_sort_keeps_original_tie_order(self):
        rows = load_rows([{"v": 1}, {"v": 1}, {"v": 1}])
        key = PhysKey((order_part(OrderKey("v", Direction.ASC, Nulls.LAST)),))
        measurement = measured_stable_sort(rows, tuple(range(3)), key)
        self.assertEqual(measurement.permutation, (0, 1, 2))
        # Equal keys need no comparisons once Timsort detects the run, but the
        # run itself must remain in input order (stability).
        self.assertEqual(
            [rows[i][ROWNO_COLUMN] for i in measurement.permutation], [0, 1, 2]
        )

    def test_desc_direction_reverses_non_null_values(self):
        rows = load_rows([{"v": 1}, {"v": 3}, {"v": 2}])
        key = PhysKey((order_part(OrderKey("v", Direction.DESC, Nulls.LAST)),))
        measurement = measured_stable_sort(rows, tuple(range(3)), key)
        self.assertEqual([rows[i]["v"] for i in measurement.permutation], [3, 2, 1])

    def test_nulls_first_and_nulls_last_are_distinct_orders(self):
        rows = load_rows([{"v": None}, {"v": 1}, {"v": None}, {"v": 0}])
        first_key = PhysKey(
            (order_part(OrderKey("v", Direction.ASC, Nulls.FIRST)),)
        )
        last_key = PhysKey(
            (order_part(OrderKey("v", Direction.ASC, Nulls.LAST)),)
        )
        first_order = measured_stable_sort(rows, tuple(range(4)), first_key).permutation
        last_order = measured_stable_sort(rows, tuple(range(4)), last_key).permutation
        self.assertEqual([rows[i]["v"] for i in first_order], [None, None, 0, 1])
        self.assertEqual([rows[i]["v"] for i in last_order], [0, 1, None, None])

    def test_desc_nulls_first_puts_null_before_descending_values(self):
        rows = load_rows([{"v": None}, {"v": 1}, {"v": 2}])
        key = PhysKey(
            (order_part(OrderKey("v", Direction.DESC, Nulls.FIRST)),)
        )
        order = measured_stable_sort(rows, tuple(range(3)), key).permutation
        self.assertEqual([rows[i]["v"] for i in order], [None, 2, 1])

    def test_rowno_tail_breaks_ties_without_splitting_equal_groups(self):
        rows = load_rows([{"v": 5}, {"v": 5}, {"v": 2}])
        key = PhysKey(
            (
                order_part(OrderKey("v", Direction.ASC, Nulls.LAST)),
                rowno_part(),
            )
        )
        order = measured_stable_sort(rows, tuple(range(3)), key).permutation
        self.assertEqual([rows[i][ROWNO_COLUMN] for i in order], [2, 0, 1])


class PartialSortTest(unittest.TestCase):
    def test_groups_are_sorted_separately_and_keep_their_positions(self):
        rows = load_rows(
            [
                {"dept": "A", "salary": 3},
                {"dept": "A", "salary": 1},
                {"dept": "B", "salary": 9},
                {"dept": "B", "salary": 4},
                {"dept": "A", "salary": 2},
            ]
        )
        # First make dept contiguous with a full sort.
        partition_key = PhysKey((partition_part("dept"), rowno_part()))
        partitioned = measured_stable_sort(rows, tuple(range(5)), partition_key)
        self.assertEqual(
            [rows[i]["dept"] for i in partitioned.permutation],
            ["A", "A", "A", "B", "B"],
        )
        sort_key = PhysKey(
            (
                partition_part("dept"),
                order_part(OrderKey("salary", Direction.ASC, Nulls.LAST)),
                rowno_part(),
            )
        )
        partial = measured_partial_sort(
            rows, partitioned.permutation, ("dept",), sort_key
        )
        ordered = [rows[i] for i in partial.permutation]
        self.assertEqual([row["dept"] for row in ordered], ["A", "A", "A", "B", "B"])
        self.assertEqual(
            [row["salary"] for row in ordered if row["dept"] == "A"], [1, 2, 3]
        )
        self.assertEqual(
            [row["salary"] for row in ordered if row["dept"] == "B"], [4, 9]
        )
        self.assertEqual(partial.group_sizes, (3, 2))
        # Comparisons never cross partition boundaries: each group is sorted
        # independently, so the total is strictly below a full 5-row sort on
        # comparable data distributions.
        full = measured_stable_sort(rows, partitioned.permutation, sort_key)
        self.assertLessEqual(partial.comparisons, full.comparisons)


class CandidateAndReuseTest(unittest.TestCase):
    def test_row_number_candidates_always_carry_rowno_tail(self):
        spec = _spec()
        keys = candidate_keys(spec)
        self.assertTrue(keys)
        for key in keys:
            self.assertEqual(key.parts[-1].role, KeyRole.ROWNO_TAIL)
            self.assertEqual(key.parts[-1].column, ROWNO_COLUMN)

    def test_group_candidates_include_tailed_and_tailless_variants(self):
        spec = _spec(
            tie_policy=TiePolicy.GROUP,
            function=WindowFunction.DENSE_RANK,
        )
        keys = candidate_keys(spec)
        self.assertTrue(any(key.parts[-1].role is KeyRole.ROWNO_TAIL for key in keys))
        self.assertTrue(any(key.parts[-1].role is KeyRole.ORDER for key in keys))

    def test_partition_permutations_are_all_offered(self):
        spec = _spec(partition_by=("a", "b"))
        keys = candidate_keys(spec)
        prefixes = {key.parts[:2] for key in keys}
        prefix_columns = {
            tuple(part.column for part in prefix) for prefix in prefixes
        }
        self.assertEqual(prefix_columns, {("a", "b"), ("b", "a")})

    def test_reuse_accepts_matching_order_and_rejects_direction_change(self):
        spec = _spec()
        matching = PhysKey(
            (
                partition_part("dept"),
                order_part(OrderKey("salary", Direction.ASC, Nulls.LAST)),
                rowno_part(),
            )
        )
        self.assertTrue(can_reuse(matching, spec))
        descending = PhysKey(
            (
                partition_part("dept"),
                order_part(OrderKey("salary", Direction.DESC, Nulls.LAST)),
                rowno_part(),
            )
        )
        self.assertFalse(can_reuse(descending, spec))

    def test_reuse_requires_rowno_tail_for_row_number_policy(self):
        spec = _spec(tie_policy=TiePolicy.ROW_NUMBER)
        tailless = PhysKey(
            (
                partition_part("dept"),
                order_part(OrderKey("salary", Direction.ASC, Nulls.LAST)),
            )
        )
        self.assertFalse(can_reuse(tailless, spec))

    def test_group_window_ignores_extra_rowno_tail(self):
        spec = _spec(
            tie_policy=TiePolicy.GROUP,
            function=WindowFunction.CUMULATIVE_COUNT,
        )
        tailed = PhysKey(
            (
                partition_part("dept"),
                order_part(OrderKey("salary", Direction.ASC, Nulls.LAST)),
                rowno_part(),
            )
        )
        self.assertTrue(can_reuse(tailed, spec))

    def test_partial_sort_key_keeps_current_partition_permutation(self):
        spec = _spec(partition_by=("b", "a"))
        current = PhysKey((partition_part("b"), partition_part("a"), rowno_part()))
        self.assertTrue(partition_prefix_match(current, spec))
        sort_key = partial_sort_key(current, spec)
        self.assertEqual(
            [part.column for part in sort_key.parts[:2]], ["b", "a"]
        )

    def test_initial_key_only_serves_empty_partition_empty_order_row_number(self):
        spec = _spec(partition_by=(), order_by=())
        self.assertTrue(can_reuse(INITIAL_KEY, spec))
        with_partition = _spec(partition_by=("dept",), order_by=())
        self.assertFalse(can_reuse(INITIAL_KEY, with_partition))

    def test_unrelated_value_types_are_rejected_instead_of_silently_ordered(self):
        from window_planning.physical import _MixedTypeError

        rows = load_rows([{"v": "a"}, {"v": 1}])
        key = PhysKey((order_part(OrderKey("v", Direction.ASC, Nulls.LAST)),))
        with self.assertRaises(_MixedTypeError):
            measured_stable_sort(rows, (0, 1), key)


class FeeScheduleTest(unittest.TestCase):
    def test_price_combines_length_and_comparison_fees(self):
        measurement = measured_stable_sort(
            load_rows([{"v": 3}, {"v": 1}, {"v": 2}]),
            (0, 1, 2),
            PhysKey((order_part(OrderKey("v", Direction.ASC, Nulls.LAST)),)),
        )
        fee = FEE.price(measurement)
        self.assertEqual(
            fee,
            FEE.length_price(3) + FEE.comparison_price(measurement.comparisons),
        )

    def test_comparison_brackets_use_smallest_sufficient_bound(self):
        self.assertEqual(FEE.comparison_price(1), 2)  # 1 <= 4
        self.assertEqual(FEE.comparison_price(5), 4)  # 4 < 5 <= 8
        self.assertEqual(FEE.comparison_price(8), 4)

    def test_equal_run_keeps_input_order_and_is_priced_from_measurement(self):
        # A single equal-value run stays in input order (stability). Timsort
        # still performs a small number of comparisons while recognising the
        # run; the fee must come from the real measurement, not an assumption.
        rows = load_rows([{"v": 1}, {"v": 1}, {"v": 1}])
        measurement = measured_stable_sort(
            rows,
            (0, 1, 2),
            PhysKey((order_part(OrderKey("v", Direction.ASC, Nulls.LAST)),)),
        )
        self.assertEqual(measurement.permutation, (0, 1, 2))
        self.assertEqual(
            FEE.price(measurement),
            FEE.length_price(3) + FEE.comparison_price(measurement.comparisons),
        )

    def test_singleton_group_is_excluded_from_group_fees(self):
        from window_planning.physical import SortMeasurement

        self.assertEqual(FEE.price(SortMeasurement((0,), 0, ())), 0)

    def test_missing_length_fee_is_loud(self):
        schedule = FeeSchedule(length_fee={2: 2}, comparison_brackets={8: 4})
        rows = load_rows([{"v": 1}, {"v": 2}, {"v": 3}])
        measurement = measured_stable_sort(
            rows,
            (0, 1, 2),
            PhysKey((order_part(OrderKey("v", Direction.ASC, Nulls.LAST)),)),
        )
        with self.assertRaises(MissingFeeError):
            schedule.price(measurement)

    def test_missing_comparison_bracket_is_loud(self):
        schedule = FeeSchedule(length_fee={3: 3}, comparison_brackets={1: 1})
        rows = load_rows([{"v": 3}, {"v": 1}, {"v": 2}, {"v": 0}])
        measurement = measured_stable_sort(
            rows,
            (0, 1, 2, 3),
            PhysKey((order_part(OrderKey("v", Direction.ASC, Nulls.LAST)),)),
        )
        with self.assertRaises(MissingFeeError):
            schedule.price(measurement)


if __name__ == "__main__":
    unittest.main()
