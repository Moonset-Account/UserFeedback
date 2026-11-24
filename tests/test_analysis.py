"""Tests for the verdict layer, the counterexamples, and their evidence."""

import unittest

from relanalysis import analyze, verify_evidence


LOSSLESS_FD = (
    ["A", "B", "C"],
    [("R1", ["A", "B"]), ("R2", ["B", "C"])],
    [("FD", ("B",), ("C",))],
)
LOSSY_NO_DEPS = (
    ["A", "B", "C"],
    [("R1", ["A", "B"]), ("R2", ["B", "C"])],
    [],
)
LOSSLESS_MVD = (
    ["A", "B", "C", "D"],
    [("R1", ["A", "B"]), ("R2", ["A", "C", "D"])],
    [("MVD", ("A",), ("B",))],
)
LOSSY_MVD_SPLIT = (
    ["A", "B", "C"],
    [("R1", ["A", "B"]), ("R2", ["A", "C"])],
    [],
)
LOSSY_MVD_DISCONNECTED = (
    ["A", "B", "C", "D"],
    [("R1", ["A", "B"]), ("R2", ["C", "D"])],
    [("MVD", ("A",), ("B",))],
)


class VerdictTests(unittest.TestCase):
    def test_fd_case_is_lossless(self):
        result = analyze(*LOSSLESS_FD)
        self.assertTrue(result.lossless)
        self.assertEqual(result.verdict, "lossless")

    def test_no_dependency_case_is_lossy(self):
        result = analyze(*LOSSY_NO_DEPS)
        self.assertFalse(result.lossless)

    def test_mvd_case_is_lossless(self):
        self.assertTrue(analyze(*LOSSLESS_MVD).lossless)

    def test_cross_split_is_lossy(self):
        self.assertFalse(analyze(*LOSSY_MVD_SPLIT).lossless)

    def test_mvd_that_does_not_connect_is_lossy(self):
        self.assertFalse(analyze(*LOSSY_MVD_DISCONNECTED).lossless)

    def test_single_subtable_covering_schema_is_lossless(self):
        result = analyze(["A", "B", "C"], [("R1", ["A", "B", "C"])], [])
        self.assertTrue(result.lossless)

    def test_lossless_evidence_carries_steps_and_witness(self):
        document = analyze(*LOSSLESS_FD).to_dict()
        section = document["lossless"]
        self.assertTrue(section["steps"])
        self.assertEqual(section["witness_row"]["A"], "a:A")
        self.assertIsNone(document["counterexample"])

    def test_lossy_evidence_carries_counterexample(self):
        document = analyze(*LOSSY_NO_DEPS).to_dict()
        section = document["counterexample"]
        self.assertTrue(section["instance"])
        self.assertTrue(section["extra_rows"])
        self.assertTrue(section["dependency_check"]["satisfied"])
        self.assertIsNone(document["lossless"])

    def test_evidence_is_json_serialisable(self):
        import json

        for case in (LOSSLESS_FD, LOSSY_NO_DEPS, LOSSLESS_MVD, LOSSY_MVD_SPLIT):
            text = json.dumps(analyze(*case).to_dict())
            self.assertTrue(text)


class CounterexampleContentTests(unittest.TestCase):
    def test_extra_row_is_documented_with_suppliers(self):
        document = analyze(*LOSSY_NO_DEPS).to_dict()
        for extra in document["counterexample"]["extra_rows"]:
            self.assertIn("row", extra)
            self.assertEqual(set(extra["suppliers"]), {"R1", "R2"})
            for suppliers in extra["suppliers"].values():
                self.assertTrue(suppliers)
                for supplied in suppliers:
                    self.assertIn("instance_row", supplied)
                    self.assertIn("projection", supplied)

    def test_chase_witness_always_appears_among_extras(self):
        for case in (LOSSY_NO_DEPS, LOSSY_MVD_SPLIT, LOSSY_MVD_DISCONNECTED):
            document = analyze(*case).to_dict()
            section = document["counterexample"]
            self.assertTrue(section["target_row_is_extra"])
            rows = [extra["row"] for extra in section["extra_rows"]]
            self.assertIn(section["target_row"], rows)

    def test_instance_satisfies_dependencies(self):
        from relanalysis import semantics
        from relanalysis.model import Schema, make_dependencies

        schema = Schema.from_columns(LOSSY_MVD_DISCONNECTED[0])
        deps = make_dependencies(schema, LOSSY_MVD_DISCONNECTED[2])
        section = analyze(*LOSSY_MVD_DISCONNECTED).to_dict()["counterexample"]
        self.assertEqual(semantics.dependency_violations(schema, section["instance"], deps), [])

    def test_instance_is_a_set_of_rows(self):
        section = analyze(*LOSSY_NO_DEPS).to_dict()["counterexample"]
        tuples = [tuple(sorted(row.items())) for row in section["instance"]]
        self.assertEqual(len(tuples), len(set(tuples)))

    def test_instance_row_count_matches(self):
        section = analyze(*LOSSY_NO_DEPS).to_dict()["counterexample"]
        self.assertEqual(section["instance_size"], len(section["instance"]))

    def test_minimised_instance_is_small(self):
        section = analyze(*LOSSY_NO_DEPS).to_dict()["counterexample"]
        # the classic 2-row counterexample for this decomposition
        self.assertLessEqual(section["instance_size"], 3)


class InterleavingEvidenceTests(unittest.TestCase):
    INTERLEAVED = (
        ["A", "B", "C", "D"],
        [("R1", ["A", "B"]), ("R2", ["A", "C"]), ("R3", ["B", "D"])],
        [("FD", ("C",), ("A",)), ("MVD", ("A",), ("B",)), ("MVD", ("B",), ("C",))],
    )

    def test_interleaving_case_is_lossless(self):
        self.assertTrue(analyze(*self.INTERLEAVED).lossless)

    def test_interleaving_is_disclosed_and_legal(self):
        document = analyze(*self.INTERLEAVED).to_dict()
        observations = document["lossless"]["interleavings"]
        self.assertTrue(observations)
        for observation in observations:
            self.assertEqual(observation["kind"], "mvd-after-fd-merge-of-mvd-row")
            for parent in observation["merged_parents"]:
                self.assertLess(parent["created_by_step"], observation["step"])
                for merge in parent["fd_merge_steps"]:
                    self.assertGreater(merge, parent["created_by_step"])
                    self.assertLess(merge, observation["step"])

    def test_verify_accepts_interleaving_evidence(self):
        document = analyze(*self.INTERLEAVED).to_dict()
        result = verify_evidence(document)
        self.assertTrue(result.accepted, result.errors)
        self.assertTrue(result.notes)


class ExplainTests(unittest.TestCase):
    def test_lossless_explanation_mentions_witness(self):
        text = analyze(*LOSSLESS_FD).explain()
        self.assertIn("verdict: lossless", text)
        self.assertIn("witness row", text)
        self.assertIn("B -> C", text)

    def test_lossy_explanation_lists_extra_rows_and_suppliers(self):
        text = analyze(*LOSSY_NO_DEPS).explain()
        self.assertIn("verdict: lossy", text)
        self.assertIn("extra joined row", text)
        self.assertIn("supplied by R1", text)
        self.assertIn("supplied by R2", text)

    def test_explanation_of_interleaving_mentions_pattern(self):
        text = analyze(*InterleavingEvidenceTests.INTERLEAVED).explain()
        self.assertIn("MVD -> FD -> MVD", text)


if __name__ == "__main__":
    unittest.main()
