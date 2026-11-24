"""Operational semantics: dependency satisfaction and natural join."""

import unittest

from relanalysis import semantics
from relanalysis.model import Schema, make_dependencies, make_subtables


class DependencySatisfactionTests(unittest.TestCase):
    def setUp(self):
        self.schema = Schema.from_columns(["A", "B", "C"])

    def rows(self, *triples):
        return [dict(zip(self.schema.columns, t)) for t in triples]

    def test_fd_satisfied(self):
        deps = make_dependencies(self.schema, [("FD", ("A",), ("B",))])
        rows = self.rows((1, "x", 10), (1, "x", 11), (2, "y", 12))
        self.assertEqual(semantics.dependency_violations(self.schema, rows, deps), [])

    def test_fd_violation_reported(self):
        deps = make_dependencies(self.schema, [("FD", ("A",), ("B",))])
        rows = self.rows((1, "x", 10), (1, "y", 11))
        violations = semantics.dependency_violations(self.schema, rows, deps)
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0]["rows"], [0, 1])

    def test_mvd_satisfied_by_swapped_rows(self):
        deps = make_dependencies(self.schema, [("MVD", ("A",), ("B",))])
        rows = self.rows((1, "x", 10), (1, "y", 11), (1, "x", 11), (1, "y", 10))
        self.assertEqual(semantics.dependency_violations(self.schema, rows, deps), [])

    def test_mvd_violation_when_swap_missing(self):
        deps = make_dependencies(self.schema, [("MVD", ("A",), ("B",))])
        rows = self.rows((1, "x", 10), (1, "y", 11))
        violations = semantics.dependency_violations(self.schema, rows, deps)
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0]["dependency"]["kind"], "MVD")
        self.assertEqual(violations[0]["rows"], [0, 1])

    def test_mvd_uses_complement_columns(self):
        schema = Schema.from_columns(["A", "B", "C", "D"])
        deps = make_dependencies(schema, [("MVD", ("A",), ("B",))])
        rows = [
            {"A": 1, "B": "x", "C": 10, "D": True},
            {"A": 1, "B": "y", "C": 20, "D": False},
            {"A": 1, "B": "x", "C": 20, "D": False},
            {"A": 1, "B": "y", "C": 10, "D": True},
        ]
        self.assertEqual(semantics.dependency_violations(schema, rows, deps), [])

    def test_empty_left_mvd_requires_all_swaps(self):
        deps = make_dependencies(self.schema, [("MVD", (), ("A",))])
        rows = self.rows((1, "x", 10), (2, "y", 11))
        # swapping A across the two rows: (1,y,11)? no: keep A from row0 and
        # R-A from row1 -> (1,y,11) missing, so violated
        self.assertTrue(semantics.dependency_violations(self.schema, rows, deps))


class JoinTests(unittest.TestCase):
    def setUp(self):
        self.schema = Schema.from_columns(["A", "B", "C"])

    def test_join_equals_relation_when_b_is_a_key(self):
        subs = make_subtables(self.schema, [("R1", ["A", "B"]), ("R2", ["B", "C"])])
        # B determines both A and C here, so the join cannot invent rows
        rows = [{"A": 1, "B": 2, "C": 3}, {"A": 4, "B": 5, "C": 6}]
        self.assertEqual(
            semantics.join_relation(self.schema, subs, rows),
            semantics.tuple_set(self.schema, rows),
        )

    def test_join_produces_extra_rows(self):
        subs = make_subtables(self.schema, [("R1", ["A", "B"]), ("R2", ["B", "C"])])
        rows = [{"A": 1, "B": 2, "C": 3}, {"A": 4, "B": 2, "C": 5}]
        extra = semantics.join_extra(self.schema, subs, rows)
        self.assertEqual(extra, {(1, 2, 5), (4, 2, 3)})

    def test_empty_relation_joins_to_nothing(self):
        subs = make_subtables(self.schema, [("R1", ["A", "B"]), ("R2", ["B", "C"])])
        self.assertEqual(semantics.join_relation(self.schema, subs, []), set())

    def test_join_is_order_independent_over_subtables(self):
        subs_a = make_subtables(self.schema, [("R1", ["A", "B"]), ("R2", ["B", "C"])])
        subs_b = make_subtables(self.schema, [("R2", ["B", "C"]), ("R1", ["A", "B"])])
        rows = [{"A": 1, "B": 2, "C": 3}, {"A": 4, "B": 2, "C": 5}]
        self.assertEqual(
            semantics.join_relation(self.schema, subs_a, rows),
            semantics.join_relation(self.schema, subs_b, rows),
        )

    def test_three_way_join(self):
        schema = Schema.from_columns(["A", "B", "C", "D"])
        subs = make_subtables(
            schema, [("R1", ["A", "B"]), ("R2", ["B", "C"]), ("R3", ["C", "D"])]
        )
        rows = [
            {"A": 1, "B": 2, "C": 3, "D": 4},
            {"A": 1, "B": 2, "C": 5, "D": 6},
        ]
        self.assertEqual(
            semantics.join_relation(schema, subs, rows), semantics.tuple_set(schema, rows)
        )

    def test_sources_for_extra_row(self):
        subs = make_subtables(self.schema, [("R1", ["A", "B"]), ("R2", ["B", "C"])])
        rows = [{"A": 1, "B": 2, "C": 3}, {"A": 4, "B": 2, "C": 5}]
        extra = (1, 2, 5)
        sources = semantics.sources_for_extra_row(self.schema, subs, rows, extra)
        self.assertEqual([s["instance_row"] for s in sources["R1"]], [0])
        self.assertEqual([s["instance_row"] for s in sources["R2"]], [1])

    def test_project_tuples(self):
        rows = [{"A": 1, "B": 2, "C": 3}]
        self.assertEqual(semantics.project_tuples(self.schema, ["B", "A"], rows), {(1, 2)})


if __name__ == "__main__":
    unittest.main()
