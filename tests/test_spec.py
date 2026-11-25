import unittest

from grouping_sets import (
    AggregateColumn,
    DuplicateColumnError,
    GroupingSetsSpec,
    SchemaError,
    UnknownColumnError,
)


class GroupingColumnTests(unittest.TestCase):
    def test_grouping_columns_are_required_and_unique(self):
        with self.assertRaises(DuplicateColumnError):
            GroupingSetsSpec(["a", "a"], [["a"]])
        with self.assertRaises(SchemaError):
            GroupingSetsSpec(["a", ""], [["a"]])
        with self.assertRaises(SchemaError):
            GroupingSetsSpec("ab", [["a"]])
        with self.assertRaises(SchemaError):
            GroupingSetsSpec([1], [[1]])

    def test_grouping_set_must_reference_declared_columns(self):
        with self.assertRaises(UnknownColumnError):
            GroupingSetsSpec(["a"], [["b"]])

    def test_same_column_twice_inside_one_set_is_rejected(self):
        with self.assertRaises(DuplicateColumnError):
            GroupingSetsSpec(["a", "b"], [["a", "a"]])

    def test_a_set_given_as_bare_string_is_rejected(self):
        with self.assertRaises(SchemaError):
            GroupingSetsSpec(["a"], ["a"])

    def test_duplicate_grouping_sets_are_kept_in_order(self):
        spec = GroupingSetsSpec(["a", "b"], [["a"], [], ["a"]])
        self.assertEqual(
            [gs.columns for gs in spec.grouping_sets], [("a",), (), ("a",)]
        )

    def test_include_grand_total_appends_empty_set(self):
        spec = GroupingSetsSpec(["a"], [["a"]], include_grand_total=True)
        self.assertEqual([gs.columns for gs in spec.grouping_sets], [("a",), ()])
        self.assertTrue(spec.grouping_sets[1].is_grand_total)

    def test_output_columns_order(self):
        spec = GroupingSetsSpec(
            ["region", "product"],
            [["region"]],
            [("count", "*", "n"), ("sum", "amount", "total")],
        )
        self.assertEqual(
            spec.output_columns, ("region", "product", "n", "total")
        )


class AggregateSpecTests(unittest.TestCase):
    def test_functions_are_case_and_space_insensitive(self):
        spec = GroupingSetsSpec(
            ["a"], [["a"]], [(" COUNT ", "*", "n"), ("Sum", "v", "s")]
        )
        self.assertEqual([a.func for a in spec.aggregates], ["count", "sum"])

    def test_unknown_function_is_rejected(self):
        with self.assertRaises(SchemaError):
            GroupingSetsSpec(["a"], [["a"]], [("avg", "v", "x")])

    def test_star_only_allowed_for_count(self):
        with self.assertRaises(SchemaError):
            GroupingSetsSpec(["a"], [["a"]], [("sum", "*", "x")])
        spec = GroupingSetsSpec(["a"], [["a"]], [("count", "*", "x")])
        self.assertEqual(spec.aggregates[0].column, "*")

    def test_aggregate_output_must_be_unique_and_not_a_grouping_column(self):
        with self.assertRaises(DuplicateColumnError):
            GroupingSetsSpec(["a"], [["a"]], [("sum", "v", "x"), ("min", "v", "x")])
        with self.assertRaises(SchemaError):
            GroupingSetsSpec(["a"], [["a"]], [("sum", "v", "a")])

    def test_two_aggregates_on_the_same_source_are_allowed(self):
        spec = GroupingSetsSpec(
            ["a"], [["a"]], [("sum", "v", "s"), ("min", "v", "m")]
        )
        self.assertEqual(len(spec.aggregates), 2)

    def test_aggregate_column_dataclass_can_be_used_directly(self):
        spec = GroupingSetsSpec(
            ["a"], [["a"]], [AggregateColumn("sum", "v", "total")]
        )
        self.assertEqual(spec.aggregates[0].output, "total")

    def test_default_output_name_is_source_column(self):
        spec = GroupingSetsSpec(["a"], [["a"]], [("sum", "v")])
        self.assertEqual(spec.aggregates[0].output, "v")

    def test_bad_aggregate_shape(self):
        for raw in ("sum", ("sum",), ("sum", "v", "x", "y"), 42):
            with self.subTest(raw=raw):
                with self.assertRaises(SchemaError):
                    GroupingSetsSpec(["a"], [["a"]], [raw])

    def test_empty_spec_has_no_rows(self):
        spec = GroupingSetsSpec(["a"], [])
        self.assertEqual(spec.grouping_sets, ())
        self.assertEqual(spec.output_columns, ("a",))


if __name__ == "__main__":
    unittest.main()
