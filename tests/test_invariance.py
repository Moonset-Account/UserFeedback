"""Invariance: permuting sub-tables and dependencies must not change anything.

The requirement is stronger than "same verdict": evidence must be identical
after canonicalisation, and every permutation must still be independently
accepted.
"""

import itertools
import unittest

from relanalysis import analyze, verify_evidence

CASES = [
    (
        ["A", "B", "C", "D"],
        [("R1", ["A", "B"]), ("R2", ["A", "C"]), ("R3", ["B", "D"])],
        [("FD", ("C",), ("A",)), ("MVD", ("A",), ("B",)), ("MVD", ("B",), ("C",))],
        "lossless",
    ),
    (
        ["A", "B", "C"],
        [("R1", ["A", "B"]), ("R2", ["B", "C"])],
        [],
        "lossy",
    ),
    (
        ["A", "B", "C", "D"],
        [("R1", ["A", "B"]), ("R2", ["B", "C"]), ("R3", ["C", "D"])],
        [("MVD", ("A",), ("B",)), ("FD", ("C",), ("D",))],
        "lossy",
    ),
    (
        ["A", "B", "C", "D"],
        [("R1", ["A", "B"]), ("R2", ["A", "C", "D"])],
        [("MVD", ("A",), ("B",))],
        "lossless",
    ),
]


class SubTableOrderTests(unittest.TestCase):
    def test_evidence_identical_under_subtable_permutations(self):
        for schema, subs, deps, verdict in CASES:
            with self.subTest(verdict=verdict, subs=subs):
                base = analyze(schema, subs, deps).to_dict()
                self.assertEqual(base["verdict"], verdict)
                for perm in itertools.permutations(subs):
                    document = analyze(schema, list(perm), deps).to_dict()
                    self.assertEqual(document, base)
                    self.assertTrue(verify_evidence(document).accepted)

    def test_evidence_identical_under_dependency_permutations(self):
        for schema, subs, deps, verdict in CASES:
            with self.subTest(verdict=verdict, deps=deps):
                base = analyze(schema, subs, deps).to_dict()
                for perm in itertools.permutations(deps):
                    document = analyze(schema, subs, list(perm)).to_dict()
                    self.assertEqual(document, base)
                    self.assertTrue(verify_evidence(document).accepted)

    def test_evidence_identical_under_combined_permutations(self):
        for schema, subs, deps, verdict in CASES:
            with self.subTest(verdict=verdict):
                base = analyze(schema, subs, deps).to_dict()
                for sub_perm in itertools.permutations(subs):
                    for dep_perm in itertools.permutations(deps):
                        document = analyze(schema, list(sub_perm), list(dep_perm)).to_dict()
                        self.assertEqual(document, base)
                        self.assertTrue(verify_evidence(document).accepted)

    def test_verdict_stable_when_schema_columns_declared_in_any_order(self):
        schema, subs, deps, verdict = CASES[0]
        base = analyze(schema, subs, deps).to_dict()
        for perm in itertools.permutations(schema):
            document = analyze(list(perm), subs, deps).to_dict()
            self.assertEqual(document["verdict"], verdict)
            self.assertTrue(verify_evidence(document).accepted)

    def test_duplicate_dependency_declarations_are_idempotent(self):
        schema, subs, deps, verdict = CASES[0]
        base = analyze(schema, subs, deps).to_dict()
        document = analyze(schema, subs, deps + deps).to_dict()
        self.assertEqual(document, base)


if __name__ == "__main__":
    unittest.main()
