import unittest

from grouping_sets import (
    GroupingSetsEngine,
    GroupingSetsSpec,
    MissingColumnError,
    SchemaError,
)
from tests.support import index_rows, row_at


def sales_spec(**kwargs):
    return GroupingSetsSpec(
        grouping_columns=["region", "product"],
        grouping_sets=[["region", "product"], ["region"], []],
        aggregates=[
            ("count", "*", "n"),
            ("count", "amount", "n_amount"),
            ("sum", "amount", "total"),
            ("min", "amount", "lowest"),
        ],
        **kwargs,
    )


ROWS = [
    {"region": "north", "product": "a", "amount": 10},
    {"region": "north", "product": "a", "amount": 4},
    {"region": "north", "product": "b", "amount": 7},
    {"region": "south", "product": "a", "amount": 1},
]


class AggregateBasicsTests(unittest.TestCase):
    def setUp(self):
        self.engine = GroupingSetsEngine(sales_spec())

    def test_single_batch_matches_hand_computation(self):
        result = self.engine.apply(adds=ROWS)
        rows = index_rows(result.rows)

        self.assertEqual(
            rows[(0, ("north", "a"))].aggregates, (2, 2, 14, 4)
        )
        self.assertEqual(rows[(0, ("north", "b"))].aggregates, (1, 1, 7, 7))
        self.assertEqual(rows[(0, ("south", "a"))].aggregates, (1, 1, 1, 1))
        self.assertEqual(
            rows[(0, ("north", "a"))].grouping_bits, (0, 0)
        )

    def test_rollup_subtotals_and_grand_total(self):
        result = self.engine.apply(adds=ROWS)
        rows = index_rows(result.rows)

        north = rows[(1, ("north", None))]
        self.assertEqual(north.aggregates, (3, 3, 21, 4))
        self.assertEqual(north.grouping_bits, (0, 1))

        grand = rows[(2, (None, None))]
        self.assertEqual(grand.aggregates, (4, 4, 22, 1))
        self.assertEqual(grand.grouping_bits, (1, 1))

    def test_count_star_counts_rows_while_count_column_skips_nulls(self):
        self.engine.apply(
            adds=[
                {"region": "n", "product": "p", "amount": None},
                {"region": "n", "product": "p", "amount": 3},
            ]
        )
        row = row_at(self.engine.current_snapshot(), 0, ("n", "p"))
        self.assertEqual(row.aggregates, (2, 1, 3, 3))

    def test_identical_rows_are_counted_separately(self):
        self.engine.apply(
            adds=[
                {"region": "n", "product": "p", "amount": 5},
                {"region": "n", "product": "p", "amount": 5},
            ]
        )
        row = row_at(self.engine.current_snapshot(), 0, ("n", "p"))
        self.assertEqual(row.aggregates, (2, 2, 10, 5))

    def test_missing_column_is_rejected(self):
        with self.assertRaises(MissingColumnError):
            self.engine.apply(adds=[{"region": "n", "amount": 1}])
        with self.assertRaises(MissingColumnError):
            self.engine.apply(adds=[{"region": "n", "product": "p"}])

    def test_extra_input_columns_are_ignored(self):
        self.engine.apply(
            adds=[{"region": "n", "product": "p", "amount": 2, "note": "x"}]
        )
        row = row_at(self.engine.current_snapshot(), 0, ("n", "p"))
        self.assertEqual(row.aggregates, (1, 1, 2, 2))

    def test_single_mapping_must_be_wrapped_in_a_list(self):
        with self.assertRaises(SchemaError):
            self.engine.apply(adds={"region": "n", "product": "p", "amount": 1})

    def test_non_mapping_row_is_rejected(self):
        with self.assertRaises(SchemaError):
            self.engine.apply(adds=[("n", "p", 1)])

    def test_none_grouping_value_forms_its_own_group(self):
        self.engine.apply(
            adds=[
                {"region": "n", "product": None, "amount": 2},
                {"region": "n", "product": "p", "amount": 3},
            ]
        )
        rows = index_rows(self.engine.current_snapshot()[0])
        self.assertEqual(rows[(0, ("n", None))].aggregates, (1, 1, 2, 2))
        self.assertEqual(rows[(0, ("n", "p"))].aggregates, (1, 1, 3, 3))

    def test_aggregate_source_may_differ_from_grouping_columns(self):
        spec = GroupingSetsSpec(
            grouping_columns=["region"],
            grouping_sets=[["region"]],
            aggregates=[("sum", "amount", "total")],
        )
        engine = GroupingSetsEngine(spec)
        engine.apply(adds=[{"region": "n", "amount": 9}])
        self.assertEqual(row_at(engine.current_snapshot(), 0, ("n",)).aggregates, (9,))


class IntegerAndFloatTests(unittest.TestCase):
    def test_integer_sum_stays_integer(self):
        spec = GroupingSetsSpec(["g"], [["g"]], [("sum", "v", "s")])
        engine = GroupingSetsEngine(spec)
        engine.apply(adds=[{"g": 1, "v": 2}, {"g": 1, "v": 3}])
        self.assertEqual(row_at(engine.current_snapshot(), 0, (1,)).aggregates, (5,))
        self.assertIsInstance(row_at(engine.current_snapshot(), 0, (1,)).aggregates[0], int)

    def test_float_sum_uses_exact_rounding(self):
        spec = GroupingSetsSpec(["g"], [["g"]], [("sum", "v", "s")])
        engine = GroupingSetsEngine(spec)
        engine.apply(adds=[{"g": 1, "v": 0.1}, {"g": 1, "v": 0.2}])
        self.assertEqual(row_at(engine.current_snapshot(), 0, (1,)).aggregates, (0.30000000000000004,))

    def test_float_and_int_mix_works(self):
        spec = GroupingSetsSpec(["g"], [["g"]], [("sum", "v", "s")])
        engine = GroupingSetsEngine(spec)
        engine.apply(adds=[{"g": 1, "v": 1}, {"g": 1, "v": 0.5}])
        self.assertEqual(row_at(engine.current_snapshot(), 0, (1,)).aggregates, (1.5,))


if __name__ == "__main__":
    unittest.main()
