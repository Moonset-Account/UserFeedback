"""Guards against the classic consistent-query-answering mistake.

A tuple can be certain even though *no single derivation* survives every
optimal repair.  These tests pin that behaviour down so a future refactor
cannot silently downgrade certainty to ``contingent``.
"""

from __future__ import annotations

import itertools
import unittest

from consistent_query.engine import STATUS_CERTAIN, STATUS_CONTINGENT, analyse
from consistent_query.examples import (
    conflicting_groups_certain_query,
    conflicting_groups_problem,
)
from consistent_query.model import Problem, Query, Row, Table
from consistent_query.verify import oracle


class MultiDerivationCertaintyTests(unittest.TestCase):
    def test_no_derivation_survives_every_repair_but_tuple_is_certain(self):
        problem = conflicting_groups_problem()
        query = conflicting_groups_certain_query()
        report = analyse(problem, query)
        self.assertEqual(report.verdict(("L", "C")).status, STATUS_CERTAIN)

        # Enumerate the global optimal repairs and the derivations.
        global_repairs = [
            frozenset().union(*combo)
            for combo in itertools.product(
                *[component.optimal_repairs for component in report.components]
            )
        ]
        from consistent_query.model import RowKey

        derivations = [
            (RowKey("L", lrid), RowKey("R", rrid))
            for lrid in ("l1", "l2")
            for rrid in ("r1", "r2")
        ]
        # A single derivation that survives all optimal repairs would mean its
        # rows are in the intersection of every repair -- there is none.
        universally_surviving = [
            derivation
            for derivation in derivations
            if all(all(key in repair for key in derivation) for repair in global_repairs)
        ]
        self.assertEqual(universally_surviving, [])

        # But every optimal repair derives ('L', 'C') through *some* pair.
        for repair in global_repairs:
            self.assertTrue(
                any(
                    frozenset(derivation) <= repair
                    for derivation in derivations
                )
            )

    def test_naive_per_derivation_rule_would_be_wrong(self):
        # If certainty were "one derivation survives every repair", the answer
        # would be contingent.  The engine must not report that.
        report = analyse(
            conflicting_groups_problem(), conflicting_groups_certain_query()
        )
        self.assertNotEqual(report.verdict(("L", "C")).status, STATUS_CONTINGENT)


class DisjointConflictUnionTests(unittest.TestCase):
    """A single table where two disjoint conflicts share a projection value."""

    def _problem(self):
        table = Table(
            name="T",
            columns=("g", "out"),
            rows=(
                Row("a1", {"g": 1, "out": "Z"}),
                Row("a2", {"g": 1, "out": "Q"}),
                Row("b1", {"g": 2, "out": "Z"}),
                Row("b2", {"g": 2, "out": "Q"}),
            ),
        )
        from consistent_query.model import Denial

        return Problem(
            tables=(table,),
            denials=(Denial("T", "T", ("g",), ("g",)),),
        )

    def test_projection_is_contingent_not_certain(self):
        problem = self._problem()
        query = Query(tables=("T",), projection=(("T", "out"),))
        report = analyse(problem, query)
        self.assertEqual(report.verdict(("Z",)).status, STATUS_CONTINGENT)
        self.assertEqual(report.verdict(("Q",)).status, STATUS_CONTINGENT)
        self.assertEqual(
            {c.answer: c.status for c in report.candidates},
            oracle(problem, query).answers,
        )


if __name__ == "__main__":
    unittest.main()
