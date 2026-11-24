"""Model-level tests: schema, sub-tables, dependencies, instances."""

import unittest

from relanalysis.errors import DataError, DependencyError, SchemaError
from relanalysis.model import (
    FD,
    MVD,
    Dependency,
    Instance,
    Schema,
    SubTable,
    make_dependencies,
    make_subtables,
)


class SchemaTests(unittest.TestCase):
    def test_columns_are_sorted_and_deduplicated(self):
        schema = Schema.from_columns(["C", "A", "B"])
        self.assertEqual(schema.columns, ("A", "B", "C"))
        self.assertEqual(len(schema), 3)

    def test_up_to_five_columns_accepted(self):
        schema = Schema.from_columns(["E", "D", "C", "B", "A"])
        self.assertEqual(len(schema.columns), 5)

    def test_six_columns_rejected(self):
        with self.assertRaises(SchemaError):
            Schema.from_columns(list("ABCDEF"))

    def test_empty_schema_rejected(self):
        with self.assertRaises(SchemaError):
            Schema.from_columns([])

    def test_duplicate_columns_rejected(self):
        with self.assertRaises(SchemaError):
            Schema.from_columns(["A", "A"])

    def test_non_string_column_rejected(self):
        with self.assertRaises(SchemaError):
            Schema.from_columns(["A", 1])

    def test_require_flags_unknown_columns(self):
        schema = Schema.from_columns(["A", "B"])
        with self.assertRaises(SchemaError):
            schema.require(["A", "Z"], "test")

    def test_hashable_and_comparable(self):
        self.assertEqual(Schema.from_columns(["A"]), Schema.from_columns(["A"]))
        self.assertNotEqual(Schema.from_columns(["A"]), Schema.from_columns(["B"]))


class SubTableTests(unittest.TestCase):
    def setUp(self):
        self.schema = Schema.from_columns(["A", "B", "C"])

    def test_make_subtables_sorts_canonically(self):
        first = make_subtables(self.schema, [("R2", ["B", "C"]), ("R1", ["A", "B"])])
        second = make_subtables(self.schema, [("R1", ["A", "B"]), ("R2", ["B", "C"])])
        self.assertEqual(first, second)
        self.assertEqual([st.name for st in first], ["R1", "R2"])

    def test_subtable_columns_must_exist(self):
        with self.assertRaises(SchemaError):
            make_subtables(self.schema, [("R1", ["A", "Z"]), ("R2", ["B", "C"])])

    def test_empty_subtable_rejected(self):
        with self.assertRaises(SchemaError):
            SubTable(name="R1", columns=[])

    def test_duplicate_names_rejected(self):
        with self.assertRaises(SchemaError):
            make_subtables(self.schema, [("R1", ["A"]), ("R1", ["B", "C"])])

    def test_columns_must_all_be_covered(self):
        with self.assertRaises(SchemaError):
            make_subtables(self.schema, [("R1", ["A", "B"])])

    def test_no_subtables_rejected(self):
        with self.assertRaises(SchemaError):
            make_subtables(self.schema, [])

    def test_mapping_form_accepted(self):
        subs = make_subtables(self.schema, {"R1": ["A", "B"], "R2": ["B", "C"]})
        self.assertEqual(len(subs), 2)


class DependencyTests(unittest.TestCase):
    def setUp(self):
        self.schema = Schema.from_columns(["A", "B", "C", "D"])

    def test_fd_parsing_and_sorting(self):
        deps = make_dependencies(self.schema, [("FD", ("C", "B"), ("D", "A"))])
        self.assertEqual(deps[0].kind, FD)
        self.assertEqual(deps[0].left, ("B", "C"))
        self.assertEqual(deps[0].right, ("A", "D"))

    def test_mvd_complete_form(self):
        deps = make_dependencies(self.schema, [("MVD", ("A",), ("B",))])
        self.assertEqual(deps[0].kind, MVD)
        self.assertEqual(deps[0].here, frozenset({"A"}))
        self.assertEqual(deps[0].there, frozenset({"B"}))

    def test_mvd_overlap_rejected(self):
        with self.assertRaises(DependencyError):
            make_dependencies(self.schema, [("MVD", ("A",), ("A", "B"))])

    def test_empty_right_side_rejected(self):
        with self.assertRaises(DependencyError):
            make_dependencies(self.schema, [("FD", ("A",), ())])

    def test_unknown_columns_rejected(self):
        with self.assertRaises(DependencyError):
            make_dependencies(self.schema, [("FD", ("A",), ("Z",))])

    def test_unknown_kind_rejected(self):
        with self.assertRaises(DependencyError):
            make_dependencies(self.schema, [("XYZ", ("A",), ("B",))])

    def test_duplicates_removed_and_sorted(self):
        deps = make_dependencies(
            self.schema,
            [
                ("MVD", ("A",), ("B",)),
                ("FD", ("A",), ("B",)),
                ("MVD", ("A",), ("B",)),
            ],
        )
        self.assertEqual(len(deps), 2)
        self.assertEqual([d.kind for d in deps], [FD, MVD])

    def test_mapping_form(self):
        dep = Dependency(**{"kind": "FD", "left": ("A",), "right": ("B",)})
        deps = make_dependencies(self.schema, [dep.as_dict()])
        self.assertEqual(deps[0], dep)

    def test_order_independent_canonical_form(self):
        one = make_dependencies(self.schema, [("FD", ("A",), ("B",)), ("MVD", ("C",), ("D",))])
        two = make_dependencies(self.schema, [("MVD", ("C",), ("D",)), ("FD", ("A",), ("B",))])
        self.assertEqual(one, two)


class InstanceTests(unittest.TestCase):
    def setUp(self):
        self.schema = Schema.from_columns(["A", "B", "C"])

    def test_set_semantics_drops_duplicate_rows(self):
        instance = Instance.from_rows(
            self.schema,
            [
                {"A": 1, "B": 2, "C": 3},
                {"A": 1, "B": 2, "C": 3},
                {"A": 1, "B": 2, "C": 4},
            ],
        )
        self.assertEqual(len(instance.rows), 2)

    def test_missing_column_rejected(self):
        with self.assertRaises(DataError):
            Instance.from_rows(self.schema, [{"A": 1, "B": 2}])

    def test_extra_column_rejected(self):
        with self.assertRaises(DataError):
            Instance.from_rows(self.schema, [{"A": 1, "B": 2, "C": 3, "D": 4}])

    def test_null_rejected(self):
        with self.assertRaises(DataError):
            Instance.from_rows(self.schema, [{"A": None, "B": 2, "C": 3}])

    def test_unhashable_value_rejected(self):
        with self.assertRaises(DataError):
            Instance.from_rows(self.schema, [{"A": [1], "B": 2, "C": 3}])

    def test_sequence_row_and_projection(self):
        instance = Instance.from_rows(self.schema, [(1, "x", True), (2, "y", False)])
        # projection columns are canonically sorted: C,A -> A,C
        self.assertEqual(instance.project(["C", "A"]), {(1, True), (2, False)})

    def test_dict_round_trip(self):
        instance = Instance.from_dicts(self.schema, [{"A": 1, "B": 2, "C": 3}])
        self.assertEqual(instance.rows_as_dicts(), [{"A": 1, "B": 2, "C": 3}])


if __name__ == "__main__":
    unittest.main()
