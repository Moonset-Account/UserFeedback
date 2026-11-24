"""Independent oracle: exhaustively enumerate small finite instances.

For at most three columns and a two-value domain we can enumerate every
relation.  The decomposition is lossless iff *every* instance satisfying the
dependencies joins back exactly.  This is an oracle that knows nothing about
tableaux or the chase, so it cross-checks the verdict independently.
"""

import itertools
import unittest

from relanalysis import analyze, semantics
from relanalysis.model import Schema, make_dependencies, make_subtables


def brute_force_lossless(schema_cols, sub_layout, dependencies, domain=(0, 1)):
    schema = Schema.from_columns(schema_cols)
    subs = make_subtables(schema, sub_layout)
    deps = make_dependencies(schema, dependencies)
    cols = schema.columns
    universe = [dict(zip(cols, values)) for values in itertools.product(domain, repeat=len(cols))]
    for mask in range(1, 1 << len(universe)):
        rows = [universe[i] for i in range(len(universe)) if mask >> i & 1]
        if semantics.dependency_violations(schema, rows, deps):
            continue
        if semantics.join_relation(schema, subs, rows) != semantics.tuple_set(schema, rows):
            return False
    return True


class BruteForceOracleTests(unittest.TestCase):
    CASES = [
        (["A", "B", "C"], [("R1", ["A", "B"]), ("R2", ["B", "C"])], []),
        (
            ["A", "B", "C"],
            [("R1", ["A", "B"]), ("R2", ["B", "C"])],
            [("FD", ("B",), ("C",))],
        ),
        (
            ["A", "B", "C"],
            [("R1", ["A", "B"]), ("R2", ["A", "C"])],
            [("MVD", ("A",), ("B",))],
        ),
        (
            ["A", "B", "C"],
            [("R1", ["A", "B"]), ("R2", ["B", "C"])],
            [("MVD", ("B",), ("A",))],
        ),
        (["A", "B", "C"], [("R1", ["A"]), ("R2", ["B"]), ("R3", ["C"])], []),
        (["A", "B", "C"], [("R1", ["A", "B"]), ("R2", ["A", "C"])], []),
        (
            ["A", "B", "C"],
            [("R1", ["A", "B"]), ("R2", ["B", "C"])],
            [("FD", ("A",), ("B",)), ("FD", ("B",), ("C",))],
        ),
        (
            ["A", "B", "C"],
            [("R1", ["A"]), ("R2", ["B"]), ("R3", ["C"])],
            [("MVD", (), ("A",))],
        ),
        (
            ["A", "B", "C"],
            [("R1", ["A", "B"]), ("R2", ["A", "C"])],
            [("MVD", ("A",), ("B",))],
        ),
        (
            ["A", "B", "C"],
            [("R1", ["A", "B"]), ("R2", ["B", "C"])],
            [("MVD", ("A",), ("B",))],
        ),
    ]

    def test_chase_matches_exhaustive_enumeration(self):
        for schema, subs, deps in self.CASES:
            with self.subTest(schema=schema, subs=subs, deps=deps):
                chase_verdict = analyze(schema, subs, deps).lossless
                oracle = brute_force_lossless(schema, subs, deps)
                self.assertEqual(chase_verdict, oracle)

    def test_counterexample_really_breaks_this_decomposition(self):
        """Every generated counterexample must reproduce the failure."""
        from relanalysis import analyze as build

        for schema, subs, deps in self.CASES:
            document = build(schema, subs, deps).to_dict()
            if document["verdict"] != "lossy":
                continue
            section = document["counterexample"]
            sch = Schema.from_columns(schema)
            sub_objs = make_subtables(sch, subs)
            dep_objs = make_dependencies(sch, deps)
            with self.subTest(subs=subs, deps=deps):
                self.assertEqual(
                    semantics.dependency_violations(sch, section["instance"], dep_objs), []
                )
                joined = semantics.join_relation(sch, sub_objs, section["instance"])
                self.assertNotEqual(joined, semantics.tuple_set(sch, section["instance"]))
                self.assertTrue(joined > semantics.tuple_set(sch, section["instance"]))


if __name__ == "__main__":
    unittest.main()
