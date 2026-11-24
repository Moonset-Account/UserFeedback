"""Compression optimality, barrier crossing and receipt boundary tests."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from compressor import (
    Barrier,
    Declaration,
    Operation,
    Receipt,
    SADD,
    Stream,
    Update,
    compress_stream,
    encoded_length,
    is_equivalent,
    split_segments,
)
from tests._helpers import assert_streams_agree_everywhere, small_declaration


class CompressionBasicTests(unittest.TestCase):
    def setUp(self):
        self.decl = small_declaration()

    def test_redundant_assignments_are_removed(self):
        stream = Stream(
            self.decl,
            (Update(2), Update(2), Update(3)),  # r0=7, r0=7, r0=5
        )
        result = compress_stream(stream)
        self.assertEqual(result.stream.messages, (Update(3),))
        self.assertTrue(is_equivalent(stream, result.stream))
        self.assertLess(result.encoded_bytes, encoded_length(stream))

    def test_identity_effect_segment_compresses_to_zero_updates(self):
        # Two opposite sadd updates are NOT identity overall, but an even
        # pair of identical self-cancelling constant assignments is.
        stream = Stream(
            self.decl,
            (Update(2), Update(3), Update(3)),  # 7 then 5 then 5
        )
        result = compress_stream(stream)
        self.assertEqual(result.stream.messages, (Update(3),))
        self.assertTrue(is_equivalent(stream, result.stream))

    def test_result_only_uses_declared_operations(self):
        stream = Stream(
            self.decl,
            (Update(0), Barrier((1,)), Update(1), Update(2)),
        )
        result = compress_stream(stream)
        for message in result.stream.messages:
            if isinstance(message, Update):
                self.assertIn(
                    message.operation,
                    range(len(self.decl.operations)),
                )
        assert_streams_agree_everywhere(self, stream, result.stream)

    def test_tie_break_by_operation_index(self):
        # Two distinct declared operations are everywhere-equivalent: both
        # assign the same constant 2 to r0.  A stream built from the
        # higher-index operation must be rewritten to the lower-index one,
        # since the encoded lengths tie and operation numbers decide order.
        decl = Declaration(
            registers=((0, 3),),
            constants=(2,),
            operations=(
                Operation("assign", target=0, constant=2),  # index 0
                Operation("assign", target=0, constant=2),  # index 1
            ),
        )
        stream = Stream(decl, (Update(1), Update(1)))
        result = compress_stream(stream)
        updates = [m for m in result.stream.messages if isinstance(m, Update)]
        self.assertEqual(updates, [Update(0)])
        self.assertTrue(is_equivalent(stream, result.stream))
        self.assertFalse(result.truncated)

    def test_identity_stream_compresses_to_zero_updates(self):
        # Two swaps of the same register pair cancel everywhere, so the
        # shortest equivalent message stream contains no updates at all.
        decl = Declaration(
            registers=((0, 3), (0, 3)),
            constants=(1,),
            operations=(
                Operation("swap", target=0, source=1),
                Operation(SADD, target=1, constant=1),
            ),
        )
        stream = Stream(decl, (Update(0), Update(0)))
        result = compress_stream(stream)
        self.assertEqual(
            [m for m in result.stream.messages if isinstance(m, Update)],
            [],
        )
        self.assertTrue(is_equivalent(stream, result.stream))

    def test_search_space_never_grows_beyond_original_length(self):
        stream = Stream(
            self.decl,
            tuple(Update(i % len(self.decl.operations)) for i in range(8)),
        )
        result = compress_stream(stream)
        update_count = sum(
            1 for m in result.stream.messages if isinstance(m, Update)
        )
        self.assertLessEqual(update_count, 8)
        self.assertTrue(is_equivalent(stream, result.stream))


class BarrierFoldingTests(unittest.TestCase):
    def setUp(self):
        self.decl = small_declaration()

    def test_updates_fold_across_irrelevant_barrier(self):
        original = Stream(
            self.decl,
            (Update(0), Barrier((1,)), Update(1)),
        )
        result = compress_stream(original)
        kinds = [type(m).__name__ for m in result.stream.messages]
        # The barrier does not observe r0, so both r0 updates may be placed
        # on either side of it; the search emits the barrier as early as
        # admissible, yielding barrier-first then both updates.
        self.assertEqual(kinds, ["Barrier", "Update", "Update"])
        self.assertTrue(is_equivalent(original, result.stream))
        assert_streams_agree_everywhere(self, original, result.stream)

    def test_review_scenario_sadd_roundtrip_irrelevant_barrier(self):
        """题面复核：饱和加后再减 + 只观察另一寄存器的屏障。

        The fold across the barrier must be allowed, while the bounded
        add/subtract pair must not be mistaken for an identity.
        """
        original = Stream(
            self.decl,
            (
                Update(0),               # r0 = clamp(r0 + 3)
                Barrier((1,)),           # observes only r1
                Update(1),               # r0 = clamp(r0 - 3)
            ),
        )
        result = compress_stream(original)

        # The barrier survives, exactly once, and keeps its observed register.
        barriers = [m for m in result.stream.messages if isinstance(m, Barrier)]
        self.assertEqual(len(barriers), 1)
        self.assertEqual(barriers[0].registers, (1,))

        # The two saturating updates cannot be deleted: they are not identity.
        updates = [m for m in result.stream.messages if isinstance(m, Update)]
        self.assertEqual(len(updates), 2)

        # Full equivalence on every legal entry state.
        assert_streams_agree_everywhere(self, original, result.stream)
        self.assertTrue(is_equivalent(original, result.stream))

        # And a stream that simply drops both updates is provably inequivalent.
        dropped = Stream(self.decl, (Barrier((1,)),))
        verdict = compare_streams_safely(original, dropped)
        self.assertFalse(verdict.equivalent)
        # High-end saturation is the first lex region where the pair fails
        # to round-trip on this non-negative domain.
        self.assertEqual(verdict.mismatch.entry, (8, -5))

    def test_relevant_barrier_blocks_folding(self):
        original = Stream(
            self.decl,
            (Update(2), Barrier((0,)), Update(3)),
        )
        result = compress_stream(original)
        # The barrier must stay between an r0-affecting update on each side.
        order = [type(m).__name__ for m in result.stream.messages]
        self.assertEqual(order, ["Update", "Barrier", "Update"])
        assert_streams_agree_everywhere(self, original, result.stream)

    def test_multiple_barriers_stay_ordered(self):
        original = Stream(
            self.decl,
            (
                Update(0),
                Barrier((1,)),
                Update(5),
                Barrier((0, 1)),
                Update(1),
            ),
        )
        result = compress_stream(original)
        barrier_indices = [
            i
            for i, m in enumerate(result.stream.messages)
            if isinstance(m, Barrier)
        ]
        self.assertEqual(len(barrier_indices), 2)
        self.assertLess(barrier_indices[0], barrier_indices[1])
        assert_streams_agree_everywhere(self, original, result.stream)


class ReceiptBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.decl = small_declaration()

    def test_receipts_cut_segments_and_remain_in_situ(self):
        stream = Stream(
            self.decl,
            (
                Update(2),
                Update(2),               # redundant within segment 1
                Receipt(7),
                Update(3),
                Update(3),               # redundant within segment 2
                Receipt(8),
            ),
        )
        segments = split_segments(stream.messages)
        self.assertEqual(len(segments), 3)  # two receipt segments + trailing

        result = compress_stream(stream)
        messages = result.stream.messages
        receipt_positions = [
            i for i, m in enumerate(messages) if isinstance(m, Receipt)
        ]
        self.assertEqual(len(receipt_positions), 2)
        # Nothing crosses a receipt: segment one keeps one update before
        # receipt 7, segment two keeps one update before receipt 8.
        self.assertEqual(
            messages,
            (Update(2), Receipt(7), Update(3), Receipt(8)),
        )
        assert_streams_agree_everywhere(self, stream, result.stream)

    def test_attached_receipt_is_emitted_as_standalone_anchor(self):
        stream = Stream(self.decl, (Update(2, receipt=9), Update(3)))
        result = compress_stream(stream)
        messages = result.stream.messages
        self.assertIn(Receipt(9), messages)
        self.assertTrue(is_equivalent(stream, result.stream))

    def test_updates_cannot_cross_receipt_even_if_globally_shorter(self):
        # r0=7 then r0=5: across the receipt both survive, even though
        # r0=5 alone would describe the final state without the receipt.
        stream = Stream(
            self.decl,
            (Update(2), Receipt(1), Update(3)),
        )
        result = compress_stream(stream)
        self.assertEqual(
            result.stream.messages,
            (Update(2), Receipt(1), Update(3)),
        )
        assert_streams_agree_everywhere(self, stream, result.stream)


def compare_streams_safely(original, candidate):
    from compressor import compare_streams

    return compare_streams(original, candidate)


if __name__ == "__main__":
    unittest.main()
