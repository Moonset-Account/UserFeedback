"""Tests for limits, error handling, and derivation bookkeeping."""

from __future__ import annotations

import unittest

from consistent_query.engine import (
    STATUS_CERTAIN,
    InfeasiblePins,
    Limits,
    RepairLimitExceeded,
    analyse,
    delete_row,
    keep_row,
)
from consistent_query.examples import (
    conflicting_groups_certain_query,
    conflicting_groups_problem,
    three_status_problem,
    three_status_query,
)
from consistent_query.model import ModelError, Problem, Query, Row, Table


class LimitTests(unittest.TestCase):
    def test_component_too_large_is_refused(self):
        rows = tuple(Row(f"r{i}", {"k": 1, "v": i}) for i in range(6))
        problem = Problem(
            tables=(Table("T", ("k", "v"), keys=(("k",),), rows=rows),)
        )
        query = Query(tables=("T",), projection=(("T", "v"),))
        with self.assertRaises(RepairLimitExceeded):
            analyse(problem, query, limits=Limits(max_component_rows=4))

    def test_too_many_optimal_repairs_is_refused(self):
        # One key group of nine rows has nine optimal repairs (keep exactly
        # one), all in a single component.
        rows = tuple(Row(f"r{i}", {"k": 1, "v": i}) for i in range(9))
        table = Table("T", ("k", "v"), keys=(("k",),), rows=rows)
        problem = Problem(tables=(table,))
        query = Query(tables=("T",), projection=(("T", "v"),))
        with self.assertRaises(RepairLimitExceeded):
            analyse(problem, query, limits=Limits(max_repairs_per_component=5))

    def test_derivation_limit(self):
        problem = conflicting_groups_problem()
        query = conflicting_groups_certain_query()
        with self.assertRaises(RepairLimitExceeded):
            analyse(problem, query, limits=Limits(max_derivations=2))

    def test_limits_must_be_positive(self):
        with self.assertRaises(ModelError):
            Limits(max_component_rows=0)


class PinTests(unittest.TestCase):
    def setUp(self):
        self.problem = three_status_problem()
        self.query = three_status_query()

    def test_delete_pin_with_delete_row_helper(self):
        report = analyse(self.problem, self.query, pins=[delete_row(("Q", "q2"))])
        self.assertEqual(report.row_status(("Q", "q2")), "never")
        self.assertEqual(report.verdict(("b", "u")).status, STATUS_CERTAIN)

    def test_pin_as_pair_with_string_mode(self):
        report = analyse(
            self.problem, self.query, pins=[(("Q", "q2"), "delete")]
        )
        self.assertEqual(report.row_status(("Q", "q2")), "never")

    def test_conflicting_pins_rejected(self):
        with self.assertRaises(ModelError):
            analyse(
                self.problem,
                self.query,
                pins=[keep_row(("Q", "q1")), delete_row(("Q", "q1"))],
            )

    def test_unknown_pin_rejected(self):
        with self.assertRaises(ModelError):
            analyse(self.problem, self.query, pins=[keep_row(("Q", "nope"))])

    def test_pins_echoed_in_report(self):
        report = analyse(self.problem, self.query, pins=[keep_row(("Q", "q1"))])
        self.assertEqual(len(report.pins), 1)
        self.assertTrue(report.pins[0].keep)


class QueryValidationTests(unittest.TestCase):
    def setUp(self):
        self.problem = three_status_problem()

    def test_unknown_table(self):
        with self.assertRaises(ModelError):
            analyse(
                self.problem,
                Query(tables=("Nope",), projection=(("Nope", "k"),)),
            )

    def test_unknown_column(self):
        with self.assertRaises(ModelError):
            analyse(
                self.problem,
                Query(tables=("P",), projection=(("P", "missing"),)),
            )

    def test_equality_table_not_in_join(self):
        with self.assertRaises(ModelError):
            analyse(
                self.problem,
                Query(
                    tables=("P",),
                    equalities=((("P", "k"), ("Q", "k")),),
                    projection=(("P", "v"),),
                ),
            )

    def test_projection_must_reference_joined_table(self):
        with self.assertRaises(ModelError):
            analyse(
                self.problem,
                Query(tables=("P",), projection=(("Q", "w"),)),
            )


class DerivationBookkeepingTests(unittest.TestCase):
    def test_derivation_count_matches_cartesian_join(self):
        # L has 2 rows, R has 2 rows, all k=1: every pair joins.
        report = analyse(
            conflicting_groups_problem(), conflicting_groups_certain_query()
        )
        verdict = report.verdict(("L", "C"))
        self.assertEqual(verdict.derivation_count, 4)

    def test_no_row_required_but_tuple_certain(self):
        report = analyse(
            conflicting_groups_problem(), conflicting_groups_certain_query()
        )
        self.assertEqual(len(report.required_rows), 0)
        self.assertEqual(report.certain_answers, (("L", "C"),))


if __name__ == "__main__":
    unittest.main()
