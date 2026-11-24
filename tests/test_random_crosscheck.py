"""Randomised cross-check of the engine against the brute-force oracle.

Every generated instance is tiny so the oracle can enumerate all survivor sets.
The test asserts agreement on

* the optimal cost,
* the set of optimal repairs (the engine stores them per component; the oracle
  enumerates them globally),
* the three-way status of every derivable answer, and
* that every witness the engine reports is a genuine optimal repair that does
  (or does not) yield the tuple.
"""

from __future__ import annotations

import itertools
import random
import unittest

from consistent_query.engine import analyse
from consistent_query.model import Denial, ForeignKey, Problem, Query, Row, Table
from consistent_query.verify import is_valid_repair, oracle, repair_cost


def _random_problem(rng: random.Random) -> tuple[Problem, Query]:
    # Two small tables with overlapping domains so joins are non-trivial.
    domain = [0, 1, 2]
    a_rows = []
    for i in range(rng.randint(1, 3)):
        a_rows.append(
            Row(
                f"a{i}",
                {"k": rng.choice(domain), "x": rng.choice(["u", "v"])},
                cost=rng.randint(1, 3),
            )
        )
    b_rows = []
    for i in range(rng.randint(1, 3)):
        b_rows.append(
            Row(
                f"b{i}",
                {"k": rng.choice(domain), "y": rng.choice(["u", "v"])},
                cost=rng.randint(1, 3),
            )
        )

    keys_a = rng.choice([(), (("k",),), (("k", "x"),)])
    keys_b = rng.choice([(), (("k",),), (("k", "y"),)])

    a = Table(name="A", columns=("k", "x"), keys=keys_a, rows=tuple(a_rows))
    b = Table(name="B", columns=("k", "y"), keys=keys_b, rows=tuple(b_rows))

    denials = []
    if rng.random() < 0.5:
        denials.append(
            Denial("A", "A", ("x",), ("x",))
        )
    if rng.random() < 0.4:
        denials.append(
            Denial("A", "B", ("x",), ("y",))
        )

    problem = Problem(tables=(a, b), denials=tuple(denials))
    equalities = ((("A", "k"), ("B", "k")),) if rng.random() < 0.8 else ()
    projection = rng.choice(
        [
            (("A", "x"), ("B", "y")),
            (("A", "k"), ("B", "y")),
            (("A", "x"),),
            (("B", "y"), ("A", "x")),
        ]
    )
    query = Query(tables=("A", "B"), equalities=equalities, projection=projection)
    return problem, query


class RandomCrossCheck(unittest.TestCase):
    def test_engine_matches_oracle(self):
        rng = random.Random(20261124)
        checked = 0
        for case in range(120):
            problem, query = _random_problem(rng)
            try:
                result = oracle(problem, query)
            except RuntimeError:
                # No valid repair at all (e.g. every subset violates a denial
                # that covers all rows).  The engine should also refuse.
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
                for answer, status in result.answers.items():
                    self.assertIn(answer, engine_status)
                    self.assertEqual(
                        engine_status[answer], status,
                        "answer %r: engine=%s oracle=%s"
                        % (answer, engine_status.get(answer), status),
                    )

                # Witnesses must be real optimal repairs at the right cost.
                for candidate in report.candidates:
                    if candidate.support is not None:
                        repair = candidate.support.repair
                        self.assertTrue(
                            is_valid_repair(problem, repair),
                            "support witness is not a valid repair",
                        )
                        self.assertEqual(
                            repair_cost(problem, repair), result.optimal_cost
                        )
                        derivation = candidate.support.derivation
                        self.assertTrue(set(derivation) <= repair)
                    if candidate.exclusion is not None:
                        repair = candidate.exclusion.repair
                        self.assertTrue(
                            is_valid_repair(problem, repair),
                            "exclusion witness is not a valid repair",
                        )
                        self.assertEqual(
                            repair_cost(problem, repair), result.optimal_cost
                        )
            checked += 1
        self.assertGreater(checked, 80)


if __name__ == "__main__":
    unittest.main()
