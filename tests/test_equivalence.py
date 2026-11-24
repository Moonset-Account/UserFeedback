"""Stream equivalence, anchor ordering and counterexample localisation."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from compressor import (
    Barrier,
    InvalidCandidate,
    Receipt,
    Stream,
    Update,
    compare_streams,
    is_equivalent,
)
from tests._helpers import assert_streams_agree_everywhere, small_declaration


class EquivalenceTests(unittest.TestCase):
    def setUp(self):
        self.decl = small_declaration()

    def test_identical_stream_is_equivalent(self):
        stream = Stream(
            self.decl,
            (Update(0), Barrier((0,)), Update(1), Barrier((1, 0))),
        )
        self.assertTrue(is_equivalent(stream, stream))

    def test_cross_barrier_fold_is_equivalent(self):
        # The barrier only observes r1; both updates only touch r0.
        original = Stream(
            self.decl,
            (Update(0), Barrier((1,)), Update(1)),
        )
        folded = Stream(
            self.decl,
            (Update(0), Update(1), Barrier((1,))),
        )
        self.assertTrue(is_equivalent(original, folded))
        assert_streams_agree_everywhere(self, original, folded)

    def test_cross_barrier_fold_blocked_when_register_observed(self):
        original = Stream(
            self.decl,
            (Update(0), Barrier((0,)), Update(1)),
        )
        folded = Stream(
            self.decl,
            (Update(0), Update(1), Barrier((0,))),
        )
        verdict = compare_streams(original, folded)
        self.assertFalse(verdict.equivalent)
        mismatch = verdict.mismatch
        self.assertEqual(mismatch.kind, "observation")
        self.assertEqual(mismatch.anchor_index, 0)
        # The smallest entry state whose barrier value differs: r0=0 gives
        # original 3 vs folded 0 (subtract after saturation clamps to 0).
        self.assertEqual(mismatch.entry, (0, -5))
        self.assertEqual(mismatch.original, (3,))
        self.assertEqual(mismatch.candidate, (0,))

    def test_receipt_order_is_strictly_preserved(self):
        original = Stream(
            self.decl,
            (
                Update(2, receipt=1),
                Update(3, receipt=2),
            ),
        )
        # The candidate swaps the two receipts' positions outright, so the
        # anchor descriptors disagree at the very first anchor.
        reordered = Stream(
            self.decl,
            (
                Update(3, receipt=2),
                Update(2, receipt=1),
            ),
        )
        verdict = compare_streams(original, reordered)
        self.assertFalse(verdict.equivalent)
        self.assertEqual(verdict.mismatch.kind, "structure")
        self.assertEqual(verdict.mismatch.anchor_index, 0)

    def test_missing_barrier_is_structure_mismatch(self):
        original = Stream(self.decl, (Update(0), Barrier((0,))))
        candidate = Stream(self.decl, (Update(0),))
        verdict = compare_streams(original, candidate)
        self.assertFalse(verdict.equivalent)
        self.assertEqual(verdict.mismatch.kind, "structure")

    def test_final_state_difference_is_reported(self):
        original = Stream(self.decl, (Update(2),))          # r0 := 7
        candidate = Stream(self.decl, (Update(3),))         # r0 := 5
        verdict = compare_streams(original, candidate)
        self.assertFalse(verdict.equivalent)
        mismatch = verdict.mismatch
        self.assertEqual(mismatch.kind, "final")
        self.assertEqual(mismatch.entry, (0, -5))
        self.assertEqual(mismatch.original, (7, -5))
        self.assertEqual(mismatch.candidate, (5, -5))

    def test_observation_difference_beats_final_difference(self):
        original = Stream(
            self.decl,
            (Update(2), Barrier((0,)), Update(0)),
        )
        candidate = Stream(
            self.decl,
            (Update(3), Barrier((0,)), Update(0)),
        )
        verdict = compare_streams(original, candidate)
        self.assertFalse(verdict.equivalent)
        self.assertEqual(verdict.mismatch.kind, "observation")
        self.assertEqual(verdict.mismatch.anchor_index, 0)

    def test_undeclared_operation_rejected(self):
        with self.assertRaises(InvalidCandidate):
            Stream(self.decl, (Update(99),))

    def test_attached_receipt_anchor_matches_standalone_receipt(self):
        original = Stream(self.decl, (Update(2, receipt=4),))
        candidate = Stream(self.decl, (Update(2), Receipt(4)))
        self.assertTrue(is_equivalent(original, candidate))
        assert_streams_agree_everywhere(self, original, candidate)

    def test_sadd_pair_never_treated_as_identity(self):
        original = Stream(self.decl, (Update(0), Update(1)))
        empty = Stream(self.decl, ())
        verdict = compare_streams(original, empty)
        self.assertFalse(verdict.equivalent)
        self.assertEqual(verdict.mismatch.kind, "final")
        # On the non-negative domain the first divergence is the high
        # saturation tail r0 in [8, 10].
        self.assertEqual(verdict.mismatch.entry, (8, -5))

    def test_barrier_register_order_matters(self):
        original = Stream(self.decl, (Barrier((0, 1)),))
        swapped = Stream(self.decl, (Barrier((1, 0)),))
        verdict = compare_streams(original, swapped)
        self.assertFalse(verdict.equivalent)
        self.assertEqual(verdict.mismatch.kind, "structure")


if __name__ == "__main__":
    unittest.main()
