"""Independent verification: acceptance and rejection of tampered evidence."""

import copy
import json
import unittest

from relanalysis import analyze, verify_evidence


LOSSLESS_FD = (
    ["A", "B", "C"],
    [("R1", ["A", "B"]), ("R2", ["B", "C"])],
    [("FD", ("B",), ("C",))],
)
LOSSY = (
    ["A", "B", "C"],
    [("R1", ["A", "B"]), ("R2", ["B", "C"])],
    [],
)
LOSSLESS_MVD = (
    ["A", "B", "C", "D"],
    [("R1", ["A", "B"]), ("R2", ["A", "C", "D"])],
    [("MVD", ("A",), ("B",))],
)
INTERLEAVED = (
    ["A", "B", "C", "D"],
    [("R1", ["A", "B"]), ("R2", ["A", "C"]), ("R3", ["B", "D"])],
    [("FD", ("C",), ("A",)), ("MVD", ("A",), ("B",)), ("MVD", ("B",), ("C",))],
)


class AcceptsGenuineEvidenceTests(unittest.TestCase):
    def test_accepts_every_genuine_verdict(self):
        for case in (LOSSLESS_FD, LOSSY, LOSSLESS_MVD, INTERLEAVED):
            with self.subTest(case=case[2]):
                document = analyze(*case).to_dict()
                result = verify_evidence(document)
                self.assertTrue(result.accepted, result.errors)
                self.assertTrue(result.notes)

    def test_accepts_after_json_round_trip(self):
        for case in (LOSSLESS_FD, LOSSY, LOSSLESS_MVD, INTERLEAVED):
            document = json.loads(json.dumps(analyze(*case).to_dict()))
            self.assertTrue(verify_evidence(document).accepted)

    def test_truthiness_matches_acceptance(self):
        good = analyze(*LOSSLESS_FD).to_dict()
        self.assertTrue(bool(verify_evidence(good)))


class RejectsForgedLosslessTests(unittest.TestCase):
    def setUp(self):
        self.document = analyze(*LOSSY).to_dict()

    def _forge(self, mutate):
        forged = copy.deepcopy(self.document)
        forged["verdict"] = "lossless"
        forged["lossless"] = {
            "seed_rows": [],
            "steps": [],
            "witness_row_id": 0,
            "witness_row": {c: f"a:{c}" for c in forged["schema"]},
            "interleavings": [],
            "argument": "trust me",
        }
        forged["counterexample"] = None
        mutate(forged)
        return verify_evidence(forged)

    def test_forged_lossless_without_steps_is_rejected(self):
        result = self._forge(lambda doc: None)
        self.assertFalse(result.accepted)

    def test_fabricated_witness_row_is_rejected(self):
        result = self._forge(lambda doc: None)
        self.assertTrue(any("witness" in error for error in result.errors))

    def test_forged_seed_rows_are_rejected(self):
        def mutate(doc):
            doc["lossless"]["seed_rows"] = [
                {"row_id": 0, "subtable": "R1", "symbols": {"A": "a:A", "B": "a:B", "C": "a:C"}},
                {"row_id": 1, "subtable": "R2", "symbols": {"A": "a:A", "B": "a:B", "C": "a:C"}},
            ]

        result = self._forge(mutate)
        self.assertFalse(result.accepted)

    def test_illegal_fd_step_is_rejected(self):
        document = analyze(*LOSSLESS_FD).to_dict()
        document["lossless"]["steps"][0]["parents"] = [0, 0]
        result = verify_evidence(document)
        self.assertFalse(result.accepted)
        self.assertTrue(result.errors)

    def test_wrong_substitution_is_rejected(self):
        document = analyze(*LOSSLESS_FD).to_dict()
        document["lossless"]["steps"][0]["substitutions"] = {"a:B": "a:A"}
        self.assertFalse(verify_evidence(document).accepted)

    def test_removing_required_fd_step_breaks_witness(self):
        document = analyze(*LOSSLESS_FD).to_dict()
        document["lossless"]["steps"] = []
        result = verify_evidence(document)
        self.assertFalse(result.accepted)

    def test_false_affected_rows_are_rejected(self):
        document = analyze(*LOSSLESS_FD).to_dict()
        document["lossless"]["steps"][0]["affected_rows"] = [1]
        self.assertFalse(verify_evidence(document).accepted)

    def test_undeclared_interleaving_is_rejected(self):
        document = analyze(*INTERLEAVED).to_dict()
        document["lossless"]["interleavings"] = []
        result = verify_evidence(document)
        self.assertFalse(result.accepted)
        self.assertTrue(any("interleaving" in error for error in result.errors))

    def test_fabricated_interleaving_is_rejected(self):
        document = analyze(*LOSSLESS_FD).to_dict()
        document["lossless"]["interleavings"].append(
            {
                "step": 1,
                "kind": "mvd-after-fd-merge-of-mvd-row",
                "dependency": "B -> C",
                "parents": [0, 1],
                "merged_parents": [
                    {"row_id": 1, "created_by_step": 99, "fd_merge_steps": [1]}
                ],
            }
        )
        self.assertFalse(verify_evidence(document).accepted)


class RejectsBadLossyEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.document = analyze(*LOSSY).to_dict()

    def test_instance_violating_dependency_is_rejected(self):
        document = copy.deepcopy(self.document)
        # both instance rows share B but differ on A, so B -> A is broken
        document["dependencies"] = [
            {"kind": "FD", "left": ["B"], "right": ["A"], "text": "B -> A"}
        ]
        result = verify_evidence(document)
        self.assertFalse(result.accepted)
        self.assertTrue(any("violates" in error for error in result.errors))

    def test_forged_extra_row_is_rejected(self):
        document = copy.deepcopy(self.document)
        document["counterexample"]["extra_rows"][0]["row"] = {
            "A": "bogus",
            "B": "bogus",
            "C": "bogus",
        }
        self.assertFalse(verify_evidence(document).accepted)

    def test_missing_extra_row_documentation_is_rejected(self):
        document = copy.deepcopy(self.document)
        document["counterexample"]["extra_rows"] = document["counterexample"]["extra_rows"][:1]
        result = verify_evidence(document)
        self.assertFalse(result.accepted)

    def test_bogus_supplier_is_rejected(self):
        document = copy.deepcopy(self.document)
        document["counterexample"]["extra_rows"][0]["suppliers"]["R1"][0]["instance_row"] = 99
        self.assertFalse(verify_evidence(document).accepted)

    def test_removing_a_row_so_join_is_exact_is_rejected(self):
        document = copy.deepcopy(self.document)
        # keep only one row: the projections then join to exactly that row
        document["counterexample"]["instance"] = document["counterexample"]["instance"][:1]
        self.assertFalse(verify_evidence(document).accepted)

    def test_lossy_claim_on_genuinely_lossless_input_is_rejected(self):
        good = analyze(*LOSSLESS_FD).to_dict()
        forged = copy.deepcopy(good)
        forged["verdict"] = "lossy"
        # reuse a bogus instance that is too small to witness anything
        forged["lossless"] = None
        forged["counterexample"] = {
            "instance": [{"A": 1, "B": 2, "C": 3}, {"A": 4, "B": 5, "C": 6}],
            "dependency_check": {"satisfied": True, "violations": []},
            "extra_rows": [
                {"row": {"A": 4, "B": 5, "C": 6}, "suppliers": {"R1": [], "R2": []}}
            ],
        }
        self.assertFalse(verify_evidence(forged).accepted)

    def test_duplicate_instance_rows_are_rejected(self):
        document = copy.deepcopy(self.document)
        rows = document["counterexample"]["instance"]
        document["counterexample"]["instance"] = rows + [rows[0]]
        self.assertFalse(verify_evidence(document).accepted)


class RejectsMalformedDocumentsTests(unittest.TestCase):
    def test_unknown_format(self):
        document = analyze(*LOSSY).to_dict()
        document["format"] = "something-else"
        result = verify_evidence(document)
        self.assertFalse(result.accepted)

    def test_unknown_verdict(self):
        document = analyze(*LOSSY).to_dict()
        document["verdict"] = "maybe"
        self.assertFalse(verify_evidence(document).accepted)

    def test_non_mapping(self):
        result = verify_evidence("not a document")
        self.assertFalse(result.accepted)

    def test_subtables_not_covering_schema(self):
        document = analyze(*LOSSY).to_dict()
        document["subtables"] = [{"name": "R1", "columns": ["A", "B"]}]
        self.assertFalse(verify_evidence(document).accepted)

    def test_unknown_dependency_columns(self):
        document = analyze(*LOSSY).to_dict()
        document["dependencies"] = [
            {"kind": "FD", "left": ["Z"], "right": ["A"], "text": "Z -> A"}
        ]
        self.assertFalse(verify_evidence(document).accepted)

    def test_lossless_document_with_counterexample_is_rejected(self):
        document = analyze(*LOSSLESS_FD).to_dict()
        document["counterexample"] = {"instance": []}
        self.assertFalse(verify_evidence(document).accepted)


class VerifyIsIndependentTests(unittest.TestCase):
    def test_verifier_does_not_import_analysis_chase(self):
        import relanalysis.evidence as evidence_module
        import inspect

        source = inspect.getsource(evidence_module)
        self.assertNotIn("from .analysis", source)
        self.assertNotIn("from .chase", source)

    def test_verifier_rejects_document_with_permuted_step_order(self):
        document = analyze(*INTERLEAVED).to_dict()
        steps = document["lossless"]["steps"]
        steps[0], steps[1] = steps[1], steps[0]
        # seq numbers are now out of order, which must be rejected
        self.assertFalse(verify_evidence(document).accepted)

    def test_tampered_final_tableau_is_rejected(self):
        document = analyze(*LOSSLESS_FD).to_dict()
        document["lossless"]["final_tableau"][0]["symbols"]["A"] = "wrong"
        self.assertFalse(verify_evidence(document).accepted)

    def test_final_tableau_missing_a_row_is_rejected(self):
        document = analyze(*LOSSLESS_MVD).to_dict()
        document["lossless"]["final_tableau"] = document["lossless"]["final_tableau"][:-1]
        self.assertFalse(verify_evidence(document).accepted)


if __name__ == "__main__":
    unittest.main()
