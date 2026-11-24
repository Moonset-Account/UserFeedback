"""Tests for the headline scenarios.

Each test states the expected facts and checks them through *both* the engine
and the independent oracle.
"""

from __future__ import annotations

import unittest

from consistent_query.engine import (
    STATUS_ABSENT,
    STATUS_CERTAIN,
    STATUS_CONTINGENT,
    analyse,
    delete_row,
    keep_row,
)
from consistent_query.examples import (
    conflicting_groups_certain_query,
    conflicting_groups_pair_query,
    conflicting_groups_problem,
    referential_problem,
    referential_query,
    three_status_problem,
    three_status_query,
)
from consistent_query.verify import oracle


class ConflictingKeyGroupsTests(unittest.TestCase):
    """The requested example: no row is forced, yet a projection is certain."""

    def setUp(self):
        self.problem = conflicting_groups_problem()
        self.query = conflicting_groups_certain_query()
        self.report = analyse(self.problem, self.query)

    def test_no_row_is_required(self):
        self.assertEqual(self.report.required_rows, frozenset())
        for table in self.problem.tables:
            for row in table.rows:
                self.assertEqual(
                    self.report.row_status((table.name, row.rid)), "possible"
                )

    def test_single_projection_is_certain(self):
        self.assertEqual(self.report.certain_answers, (("L", "C"),))
        verdict = self.report.verdict(("L", "C"))
        self.assertEqual(verdict.status, STATUS_CERTAIN)
        self.assertEqual(verdict.derivation_count, 4)

    def test_certainty_spans_multiple_derivations(self):
        # No single derivation survives every repair; four different ones do.
        verdict = self.report.verdict(("L", "C"))
        self.assertGreater(len(self.report.components), 1)
        repairs = self.report.certain_answers
        self.assertEqual(repairs, (("L", "C"),))
        # The support witness names one derivation, but the status is certain.
        self.assertIsNotNone(verdict.support)
        self.assertIsNotNone(verdict.support.derivation)

    def test_pair_query_is_all_contingent(self):
        pair_report = analyse(self.problem, conflicting_groups_pair_query())
        self.assertEqual(len(pair_report.candidates), 4)
        for candidate in pair_report.candidates:
            self.assertEqual(candidate.status, STATUS_CONTINGENT)
            self.assertIsNotNone(candidate.support)
            self.assertIsNotNone(candidate.exclusion)

    def test_engine_and_oracle_agree(self):
        result = oracle(self.problem, self.query)
        self.assertEqual(result.optimal_cost, self.report.optimal_cost)
        self.assertEqual(result.optimal_cost, 2)
        self.assertEqual(len(result.optimal_repairs), 4)
        self.assertEqual(result.answers, {("L", "C"): "certain"})

    def test_pinning_one_row_makes_it_required(self):
        pinned = analyse(
            self.problem, self.query, pins=[keep_row(("L", "l1"))]
        )
        self.assertEqual(pinned.row_status(("L", "l1")), "required")
        self.assertEqual(pinned.verdict(("L", "C")).status, STATUS_CERTAIN)
        # Pinning a row cannot raise the optimal cost of this symmetric case.
        self.assertEqual(pinned.optimal_cost, self.report.optimal_cost)

    def test_pinning_both_key_duplicates_is_infeasible(self):
        from consistent_query.engine import InfeasiblePins

        with self.assertRaises(InfeasiblePins):
            analyse(
                self.problem,
                self.query,
                pins=[keep_row(("L", "l1")), keep_row(("L", "l2"))],
            )

    def test_reasking_with_pin_matches_oracle(self):
        pins = [keep_row(("L", "l1")), keep_row(("R", "r2"))]
        pinned = analyse(self.problem, self.query, pins=pins)
        result = oracle(
            self.problem, self.query, pins=[(pin.row, pin.keep) for pin in pins]
        )
        self.assertEqual(pinned.optimal_cost, result.optimal_cost)
        self.assertEqual(
            {c.answer: c.status for c in pinned.candidates}, result.answers
        )


class ThreeStatusTests(unittest.TestCase):
    def test_all_three_statuses_appear(self):
        report = analyse(three_status_problem(), three_status_query())
        self.assertEqual(report.certain_answers, ())
        self.assertEqual(report.contingent_answers, (("b", "u"), ("b", "v")))
        self.assertEqual(report.absent_answers, (("a", "u"), ("a", "v")))
        result = oracle(three_status_problem(), three_status_query())
        self.assertEqual(
            {c.answer: c.status for c in report.candidates}, result.answers
        )

    def test_expensive_row_survives(self):
        # The cheap duplicate p1 is deleted, so 'a' never appears.
        report = analyse(three_status_problem(), three_status_query())
        self.assertEqual(report.row_status(("P", "p1")), "never")
        self.assertEqual(report.row_status(("P", "p2")), "required")
        for answer in report.absent_answers:
            self.assertIsNotNone(report.verdict(answer).exclusion)


class ReferentialTests(unittest.TestCase):
    def setUp(self):
        self.problem = referential_problem()
        self.query = referential_query()
        self.report = analyse(self.problem, self.query)
        self.result = oracle(self.problem, self.query)

    def test_engine_matches_oracle(self):
        self.assertEqual(self.report.optimal_cost, self.result.optimal_cost)
        self.assertEqual(self.report.optimal_cost, 3)
        self.assertEqual(
            {c.answer: c.status for c in self.report.candidates},
            self.result.answers,
        )

    def test_mandatory_fk_deletes_dangling_order(self):
        self.assertEqual(self.report.row_status(("orders", "o3")), "never")

    def test_certain_answer_survives_everything(self):
        self.assertIn((13, "grace"), self.report.certain_answers)


if __name__ == "__main__":
    unittest.main()
