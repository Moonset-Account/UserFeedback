"""Randomised cross-check including foreign keys and denial rules.

Same contract as :mod:`tests.test_random_crosscheck`, but instances now carry
mandatory and optional foreign keys, so the solver must also reason about
parent survival.  The oracle re-derives referential validity from scratch.
"""

from __future__ import annotations

import itertools
import random
import unittest

from consistent_query.engine import analyse
from consistent_query.model import Denial, ForeignKey, Problem, Query, Row, Table
from consistent_query.verify import is_valid_repair, oracle, repair_cost


def _random_fk_problem(rng: random.Random) -> tuple[Problem, Query]:
    domain = [0, 1, 2]

    parent_rows = []
    for i in range(rng.randint(1, 3)):
        parent_rows.append(
            Row(
                f"p{i}",
                {"id": rng.choice(domain), "tag": rng.choice(["a", "b"])},
                cost=rng.randint(1, 3),
            )
        )
    child_rows = []
    for i in range(rng.randint(1, 3)):
        child_rows.append(
            Row(
                f"c{i}",
                {
                    "pid": rng.choice(domain + [None]),
                    "tag": rng.choice(["a", "b"]),
                },
                cost=rng.randint(1, 3),
            )
        )

    parent = Table(
        name="P",
        columns=("id", "tag"),
        keys=(("id",),),
        rows=tuple(parent_rows),
    )
    child = Table(
        name="C",
        columns=("pid", "tag"),
        rows=tuple(child_rows),
    )

    foreign_keys = ()
    if rng.random() < 0.8:
        foreign_keys = (
            ForeignKey(
                "C",
                ("pid",),
                "P",
                ("id",),
                mandatory=rng.random() < 0.6,
            ),
        )

    denials = []
    if rng.random() < 0.4:
        denials.append(Denial("C", "C", ("tag",), ("tag",)))
    if rng.random() < 0.3:
        denials.append(Denial("P", "P", ("tag",), ("tag",)))

    problem = Problem(tables=(parent, child), foreign_keys=foreign_keys,
                      denials=tuple(denials))
    equalities = ((("C", "pid"), ("P", "id")),) if rng.random() < 0.9 else ()
    projection = rng.choice(
        [
            (("C", "tag"), ("P", "tag")),
            (("P", "id"),),
            (("C", "pid"),),
        ]
    )
    query = Query(tables=("C", "P"), equalities=equalities, projection=projection)
    return problem, query


class RandomForeignKeyCrossCheck(unittest.TestCase):
    def test_engine_matches_oracle(self):
        rng = random.Random(424242)
        checked = 0
        for case in range(150):
            problem, query = _random_fk_problem(rng)
            try:
                result = oracle(problem, query)
            except RuntimeError:
                with self.assertRaises(Exception):
                    analyse(problem, query)
                continue

            report = analyse(problem, query)
            with self.subTest(case=case):
                self.assertEqual(report.optimal_cost, result.optimal_cost)

                engine_repairs = {
                    frozenset().union(*combination)
                    for combination in itertools.product(
                        *[component.optimal_repairs for component in report.components]
                    )
                }
                self.assertEqual(engine_repairs, set(result.optimal_repairs))

                engine_status = {
                    candidate.answer: candidate.status
                    for candidate in report.candidates
                }
                self.assertEqual(engine_status, result.answers)

                for candidate in report.candidates:
                    for witness in (candidate.support, candidate.exclusion):
                        if witness is None:
                            continue
                        self.assertTrue(
                            is_valid_repair(problem, witness.repair),
                            "witness is not a valid repair",
                        )
                        self.assertEqual(
                            repair_cost(problem, witness.repair),
                            result.optimal_cost,
                        )
            checked += 1
        self.assertGreater(checked, 90)


if __name__ == "__main__":
    unittest.main()
