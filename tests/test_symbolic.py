"""Differential tests: symbolic piecewise engine vs concrete reference."""

import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from compressor import (
    ASSIGN,
    COND,
    SADD,
    SWAP,
    Declaration,
    Operation,
    execute_concrete,
    initial_state,
)
from tests._helpers import all_entry_states


class SymbolicConcreteTests(unittest.TestCase):
    def setUp(self):
        self.decl = Declaration(
            registers=((0, 6), (-4, 4), (100, 103)),
            constants=(-7, -3, -1, 0, 1, 2, 5, 100, 101),
            operations=(
                Operation(ASSIGN, target=0, constant=5),
                Operation(ASSIGN, target=1, constant=-3),
                Operation(SWAP, target=0, source=1),
                Operation(SWAP, target=2, source=0),
                Operation(SADD, target=0, constant=2),
                Operation(SADD, target=0, constant=-3),
                Operation(SADD, target=1, constant=-7),
                Operation(SADD, target=2, constant=-1),
                Operation(
                    COND, target=0, source=1, low=-3, high=0,
                    constant=2, action=SADD,
                ),
                Operation(
                    COND, target=1, source=0, low=1, high=5,
                    constant=-1, action=SADD,
                ),
                Operation(
                    COND, target=0, source=2, low=100, high=101,
                    constant=5, action=ASSIGN,
                ),
            ),
        )

    def test_all_single_operations_agree(self):
        state0 = initial_state(self.decl)
        for op_index, op in enumerate(self.decl.operations):
            symbolic = state0.apply(op)
            for entry in all_entry_states(self.decl):
                expected = execute_concrete(self.decl, entry, (op_index,))
                self.assertEqual(symbolic.concrete(entry), expected)

    def test_random_sequences_agree(self):
        rng = random.Random(20261124)
        nops = len(self.decl.operations)
        for trial in range(120):
            length = rng.randint(1, 9)
            seq = tuple(rng.randrange(nops) for _ in range(length))
            symbolic = initial_state(self.decl)
            for op_index in seq:
                symbolic = symbolic.apply(self.decl.operations[op_index])
            for entry in all_entry_states(self.decl):
                expected = execute_concrete(self.decl, entry, seq)
                self.assertEqual(
                    symbolic.concrete(entry),
                    expected,
                    msg=f"seq={seq} entry={entry}",
                )

    def test_partition_covers_every_entry_state(self):
        rng = random.Random(7)
        state = initial_state(self.decl)
        for _ in range(8):
            state = state.apply(
                self.decl.operations[rng.randrange(len(self.decl.operations))]
            )
        covered = 0
        for entry in all_entry_states(self.decl):
            state.concrete(entry)  # raises if no piece covers it
            covered += 1
        self.assertGreater(covered, 0)

    def test_canonical_merge_does_not_change_semantics(self):
        rng = random.Random(11)
        state = initial_state(self.decl)
        for _ in range(8):
            state = state.apply(
                self.decl.operations[rng.randrange(len(self.decl.operations))]
            )
        canon = state.canonical()
        for entry in all_entry_states(self.decl):
            self.assertEqual(state.concrete(entry), canon.concrete(entry))


class SaturationBoundaryTests(unittest.TestCase):
    def test_sadd_then_subtract_is_not_identity(self):
        decl = Declaration(
            registers=((0, 10),),
            constants=(3, -3),
            operations=(
                Operation(SADD, target=0, constant=3),
                Operation(SADD, target=0, constant=-3),
            ),
        )
        identity = initial_state(decl)
        combined = identity.apply(decl.operations[0]).apply(decl.operations[1])
        # Low-side saturation happens to round-trip (clamp(x+3)-3 == x for
        # x >= 0); high-side saturation loses information permanently.
        divergence = identity.divergence_boxes(combined)
        self.assertEqual(divergence, [((8, 10),)])
        self.assertEqual(execute_concrete(decl, (0,), (0, 1)), (0,))
        self.assertEqual(execute_concrete(decl, (1,), (0, 1)), (1,))
        self.assertEqual(execute_concrete(decl, (9,), (0, 1)), (7,))
        self.assertEqual(execute_concrete(decl, (10,), (0, 1)), (7,))

    def test_sadd_roundtrip_breaks_on_both_sides(self):
        # Two independent round trips, one driven negative (low saturation)
        # and one positive (high saturation), prove both ends are modelled.
        decl = Declaration(
            registers=((-10, 10),),
            constants=(3, -3),
            operations=(
                Operation(SADD, target=0, constant=3),
                Operation(SADD, target=0, constant=-3),
            ),
        )
        identity = initial_state(decl)
        # Negative-first: -3 then +3 loses the low end.
        neg_first = identity.apply(decl.operations[1]).apply(decl.operations[0])
        self.assertEqual(identity.divergence_boxes(neg_first), [((-10, -8),)])
        self.assertEqual(execute_concrete(decl, (-10,), (1, 0)), (-7,))
        # Positive-first: +3 then -3 loses the high end.
        pos_first = identity.apply(decl.operations[0]).apply(decl.operations[1])
        self.assertEqual(identity.divergence_boxes(pos_first), [((8, 10),)])
        self.assertEqual(execute_concrete(decl, (10,), (0, 1)), (7,))

    def test_large_value_range_sadd_boundaries(self):
        decl = Declaration(
            registers=((-(10 ** 12), 10 ** 12),),
            constants=(10 ** 9, -(10 ** 9)),
            operations=(
                Operation(SADD, target=0, constant=10 ** 9),
                Operation(SADD, target=0, constant=-(10 ** 9)),
            ),
        )
        plus = initial_state(decl).apply(decl.operations[0])
        # Adding a positive constant to a domain whose lower bound is far
        # below zero never underflows, so only a linear region and a high
        # saturation tail appear; boundary arithmetic stays exact.
        piece_boxes = [box for box, _ in plus.pieces]
        self.assertEqual(
            piece_boxes,
            [
                ((-(10 ** 12), 10 ** 12 - 10 ** 9),),
                ((10 ** 12 - 10 ** 9 + 1, 10 ** 12),),
            ],
        )
        combined = plus.apply(decl.operations[1])
        # Only the high-saturation tail diverges from the identity mapping.
        divergence = initial_state(decl).divergence_boxes(combined)
        self.assertEqual(
            divergence,
            [((10 ** 12 - 10 ** 9 + 1, 10 ** 12),)],
        )

    def test_conditional_guard_boundaries(self):
        decl = Declaration(
            registers=((0, 10), (0, 10)),
            constants=(2, 5, 9),
            operations=(
                Operation(
                    COND, target=0, source=1, low=2, high=5,
                    constant=9, action=ASSIGN,
                ),
            ),
        )
        state = initial_state(decl).apply(decl.operations[0])
        for r0, r1 in all_entry_states(decl):
            value = state.concrete((r0, r1))[0]
            if 2 <= r1 <= 5:
                self.assertEqual(value, 9)
            else:
                self.assertEqual(value, r0)


if __name__ == "__main__":
    unittest.main()
