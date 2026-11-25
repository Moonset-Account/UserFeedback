import io
import unittest

from grouping_sets import (
    BatchResult,
    GroupingSetsEngine,
    GroupingSetsSpec,
    ResultRow,
    diff_snapshots,
    format_rows,
    make_row,
    rows_as_dicts,
)


class PublicHelperTests(unittest.TestCase):
    def setUp(self):
        self.spec = GroupingSetsSpec(
            ["region", "product"],
            [["region"], []],
            aggregates=[("count", "*", "n"), ("sum", "amount", "total")],
        )
        self.engine = GroupingSetsEngine(self.spec)
        self.result = self.engine.apply(
            adds=[{"region": "n", "product": "a", "amount": 3}]
        )

    def test_rows_as_dicts_expands_every_row(self):
        dicts = rows_as_dicts(self.spec, self.result.rows)
        self.assertEqual(len(dicts), 2)
        self.assertEqual(set(dicts[0]), {"region", "product", "n", "total", "__grouping__", "__set_index__"})
        self.assertEqual(dicts[0]["__grouping__"], (0, 1))
        self.assertEqual(dicts[1]["__set_index__"], 1)

    def test_format_rows_returns_a_table_with_headers(self):
        text = format_rows(self.spec, self.result.rows)
        lines = text.splitlines()
        self.assertIn("region", lines[0])
        self.assertIn("grouping", lines[0])
        self.assertEqual(len(lines), 2 + len(self.result.rows))

    def test_result_row_grouping_property_for_duplicate_bits(self):
        row = make_row(0, ("x",), (0, 0), (1,))
        self.assertEqual(row.grouping, 0)
        row = make_row(0, ("x",), (1, 1), (1,))
        self.assertEqual(row.grouping, 3)

    def test_diff_snapshots_of_identical_snapshots_is_empty(self):
        upserts, deletes = diff_snapshots(
            self.result.snapshot, self.result.snapshot
        )
        self.assertEqual((upserts, deletes), ((), ()))

    def test_result_rows_are_hashable_and_comparable(self):
        row = ResultRow(0, ("a",), (0,), (1, 2))
        self.assertEqual(row, ResultRow(0, ("a",), (0,), (1, 2)))
        self.assertEqual(len({row, ResultRow(0, ("a",), (0,), (1, 2))}), 1)

    def test_batch_result_changed_flag(self):
        empty = BatchResult(0, (), (), ((), ()))
        self.assertFalse(empty.changed)
        row = make_row(0, ("a",), (0,), (1,))
        self.assertTrue(BatchResult(0, (row,), (), ((), ())).changed)

    def test_engine_repr_does_not_crash(self):
        self.assertIn("GroupingSetsSpec", repr(self.spec))


class ExceptionHierarchyTests(unittest.TestCase):
    def test_all_errors_subclass_grouping_sets_error(self):
        import grouping_sets as gs

        for name in (
            "SchemaError",
            "MissingColumnError",
            "UnknownColumnError",
            "DuplicateColumnError",
            "UnmatchedRetractionError",
            "NonFiniteValueError",
            "UnhashableGroupingValueError",
            "AggregateTypeError",
        ):
            with self.subTest(name=name):
                self.assertTrue(issubclass(getattr(gs, name), gs.GroupingSetsError))
                self.assertTrue(issubclass(getattr(gs, name), ValueError))


if __name__ == "__main__":
    unittest.main()
