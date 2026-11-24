"""Tests for input validation in :mod:`consistent_query.model`."""

from __future__ import annotations

import unittest

from consistent_query.model import (
    Denial,
    ForeignKey,
    ModelError,
    Problem,
    Query,
    Row,
    RowKey,
    Table,
)


class RowTests(unittest.TestCase):
    def test_cost_must_be_positive(self):
        for bad in (0, -1):
            with self.assertRaises(ModelError):
                Row("r", {"k": 1}, cost=bad)

    def test_cost_must_be_int_not_bool(self):
        with self.assertRaises(ModelError):
            Row("r", {"k": 1}, cost=True)  # type: ignore[arg-type]

    def test_cost_must_be_int(self):
        with self.assertRaises(ModelError):
            Row("r", {"k": 1}, cost=1.5)  # type: ignore[arg-type]

    def test_row_id_non_empty(self):
        with self.assertRaises(ModelError):
            Row("", {"k": 1})

    def test_values_are_copied(self):
        original = {"k": 1}
        row = Row("r", original)
        original["k"] = 2
        self.assertEqual(row.values, {"k": 1})


class TableTests(unittest.TestCase):
    def test_duplicate_row_ids_rejected(self):
        with self.assertRaises(ModelError):
            Table(
                name="t",
                columns=("k",),
                rows=(Row("a", {"k": 1}), Row("a", {"k": 2})),
            )

    def test_row_must_have_exact_columns(self):
        with self.assertRaises(ModelError):
            Table(name="t", columns=("k",), rows=(Row("a", {"k": 1, "x": 2}),))
        with self.assertRaises(ModelError):
            Table(name="t", columns=("k", "x"), rows=(Row("a", {"k": 1}),))

    def test_null_in_key_rejected(self):
        with self.assertRaises(ModelError):
            Table(
                name="t",
                columns=("k",),
                keys=(("k",),),
                rows=(Row("a", {"k": None}),),
            )

    def test_key_must_reference_known_columns(self):
        with self.assertRaises(ModelError):
            Table(name="t", columns=("k",), keys=(("x",),))

    def test_bare_string_columns_rejected(self):
        with self.assertRaises(ModelError):
            Table(name="t", columns="k")  # type: ignore[arg-type]


class ProblemTests(unittest.TestCase):
    def _simple(self):
        parent = Table(name="p", columns=("id",), keys=(("id",),),
                       rows=(Row("p1", {"id": 1}),))
        child = Table(name="c", columns=("pid",),
                      rows=(Row("c1", {"pid": 1}),))
        return parent, child

    def test_foreign_key_must_target_declared_key(self):
        parent, child = self._simple()
        bad = ForeignKey("c", ("pid",), "p", ("id",))
        with self.assertRaises(ModelError):
            Problem(tables=(child, Table(name="p", columns=("id",),
                                         rows=(Row("p1", {"id": 1}),))),
                    foreign_keys=(bad,))

    def test_foreign_key_arity_mismatch(self):
        with self.assertRaises(ModelError):
            ForeignKey("c", ("a", "b"), "p", ("id",))

    def test_duplicate_table_names(self):
        t = Table(name="t", columns=("k",), rows=(Row("a", {"k": 1}),))
        with self.assertRaises(ModelError):
            Problem(tables=(t, t))

    def test_query_rejects_repeated_table(self):
        with self.assertRaises(ModelError):
            Query(tables=("t", "t"), projection=(("t", "k"),),)

    def test_query_requires_projection(self):
        with self.assertRaises(ModelError):
            Query(tables=("t",), projection=())

    def test_row_key_coercion(self):
        self.assertEqual(RowKey("t", "r"), RowKey("t", "r"))
        self.assertLess(RowKey("a", "z"), RowKey("b", "a"))


if __name__ == "__main__":
    unittest.main()
