"""Randomised differential and optimality checks over small domains."""

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
    Barrier,
    Declaration,
    Operation,
    Receipt,
    Stream,
    Update,
    compare_streams,
    compress_stream,
    is_equivalent,
)
from tests._helpers import all_entry_states, assert_streams_agree_everywhere


def build_random_declaration(rng):
    nregs = rng.randint(1, 4)
    span = rng.randint(2, 6)
    registers = []
    for _ in range(nregs):
        lo = rng.randint(-6, 0)
        registers.append((lo, lo + span - 1))
    constants = tuple(sorted(set(rng.randint(-4, 4) for _ in range(rng.randint(2, 6)))))
    operations = []
    for _ in range(rng.randint(2, 6)):
        kind = rng.choice((ASSIGN, SADD, SWAP, COND))
        target = rng.randrange(nregs)
        if kind == ASSIGN:
            operations.append(
                Operation(ASSIGN, target=target, constant=rng.choice(constants))
            )
        elif kind == SADD:
            operations.append(
                Operation(SADD, target=target, constant=rng.choice(constants))
            )
        elif kind == SWAP:
            if nregs < 2:
                operations.append(
                    Operation(SADD, target=target, constant=rng.choice(constants))
                )
            else:
                source = rng.randrange(nregs - 1)
                if source >= target:
                    source += 1
                operations.append(Operation(SWAP, target=target, source=source))
        else:
            if nregs < 2:
                operations.append(
                    Operation(ASSIGN, target=target, constant=rng.choice(constants))
                )
                continue
            low = rng.choice(constants)
            high = rng.choice(constants)
            if low > high:
                low, high = high, low
            operations.append(
                Operation(
                    COND,
                    target=target,
                    source=rng.randrange(nregs),
                    low=low,
                    high=high,
                    constant=rng.choice(constants),
                    action=rng.choice((ASSIGN, SADD)),
                )
            )
    return Declaration(
        registers=tuple(registers),
        constants=constants,
        operations=tuple(operations),
    )


def build_random_stream(decl, rng, max_updates=8):
    messages = []
    remaining = rng.randint(1, max_updates)
    receipt_counter = 0
    while remaining > 0:
        choice = rng.random()
        if choice < 0.65:
            op_index = rng.randrange(len(decl.operations))
            if rng.random() < 0.2:
                receipt_counter += 1
                messages.append(Update(op_index, receipt=receipt_counter))
            else:
                messages.append(Update(op_index))
            remaining -= 1
        elif choice < 0.9:
            k = rng.randint(1, decl.nregs)
            registers = tuple(rng.sample(range(decl.nregs), k))
            if rng.random() < 0.15:
                receipt_counter += 1
                messages.append(Barrier(registers, receipt=receipt_counter))
            else:
                messages.append(Barrier(registers))
        else:
            receipt_counter += 1
            messages.append(Receipt(receipt_counter))
    return Stream(decl, tuple(messages))


class RandomisedCompressionTests(unittest.TestCase):
    def test_random_streams_compress_equivalently(self):
        rng = random.Random(20261124)
        for trial in range(80):
            decl = build_random_declaration(rng)
            stream = build_random_stream(decl, rng)
            result = compress_stream(stream, node_cap=50_000)
            self.assertTrue(
                is_equivalent(stream, result.stream),
                msg=f"trial {trial}: {stream.messages} -> {result.stream.messages}",
            )
            assert_streams_agree_everywhere(self, stream, result.stream)
            self.assertLessEqual(result.update_count, sum(
                1 for m in stream.messages if isinstance(m, Update)
            ))
            from compressor import message_body_length

            def update_body(messages):
                return sum(
                    message_body_length(m)
                    for m in messages
                    if isinstance(m, Update)
                )

            # Anchors (barriers/receipts) are preserved one-for-one, so only
            # the update-message bytes are subject to minimisation.
            self.assertLessEqual(
                update_body(result.stream.messages),
                update_body(stream.messages),
            )

    def test_brute_force_optimality_on_tiny_instances(self):
        """Exhaustive BFS over all short sequences proves byte optimality."""
        rng = random.Random(4242)
        import itertools

        for trial in range(25):
            decl = build_random_declaration(rng)
            nops = len(decl.operations)
            length = rng.randint(1, 4)
            seq = tuple(rng.randrange(nops) for _ in range(length))
            stream = Stream(decl, tuple(Update(i) for i in seq))
            result = compress_stream(stream, node_cap=200_000)

            # Enumerate every sequence no longer than the original and find
            # the shortest equivalent one independently; among equal lengths
            # keep the lexicographically smallest operation tuple.
            best_length = length
            best_tuple = seq
            for candidate_length in range(0, length + 1):
                equivalents = []
                for candidate_seq in itertools.product(
                    range(nops), repeat=candidate_length
                ):
                    candidate = Stream(
                        decl, tuple(Update(i) for i in candidate_seq)
                    )
                    if is_equivalent(stream, candidate):
                        equivalents.append(candidate_seq)
                if equivalents:
                    best_length = candidate_length
                    best_tuple = min(equivalents)
                    break
            actual = tuple(
                m.operation
                for m in result.stream.messages
                if isinstance(m, Update)
            )
            self.assertEqual(
                (len(actual), actual),
                (best_length, best_tuple),
                msg=f"trial {trial} seq={seq}",
            )

    def test_counterexample_entry_is_lexicographically_minimal(self):
        rng = random.Random(31337)
        checked = 0
        for _ in range(40):
            decl = build_random_declaration(rng)
            stream = build_random_stream(decl, rng, max_updates=5)
            # Build a candidate by mutating one random update.
            messages = list(stream.messages)
            update_positions = [
                i for i, m in enumerate(messages) if isinstance(m, Update)
            ]
            if not update_positions:
                continue
            pos = rng.choice(update_positions)
            original_op = messages[pos].operation
            alternatives = [
                i for i in range(len(decl.operations)) if i != original_op
            ]
            if not alternatives:
                continue
            mutated = Update(
                rng.choice(alternatives),
                receipt=messages[pos].receipt,
            )
            messages[pos] = mutated
            candidate = Stream(decl, tuple(messages))
            verdict = compare_streams(stream, candidate)
            if verdict.equivalent:
                continue
            checked += 1
            mismatch = verdict.mismatch
            if mismatch.entry is None:
                continue
            # Exhaustively verify no lex-smaller entry state diverges earlier.
            states = list(all_entry_states(decl))
            from compressor import concrete_replay

            for entry in states:
                if entry >= mismatch.entry:
                    break
                seen_a, final_a = concrete_replay(decl, entry, stream.messages)
                seen_b, final_b = concrete_replay(decl, entry, candidate.messages)
                self.assertEqual(
                    (seen_a, final_a),
                    (seen_b, final_b),
                    msg=f"smaller counterexample exists: {entry} < {mismatch.entry}",
                )
        self.assertGreater(checked, 5)

    def test_receipts_never_crossed_under_random_interleaving(self):
        rng = random.Random(9001)
        for _ in range(30):
            decl = build_random_declaration(rng)
            stream = build_random_stream(decl, rng, max_updates=8)
            if not any(
                isinstance(m, (Receipt,)) or
                (isinstance(m, (Update, Barrier)) and m.receipt is not None)
                for m in stream.messages
            ):
                continue
            result = compress_stream(stream, node_cap=50_000)

            def receipt_ids(messages):
                ids = []
                for m in messages:
                    if isinstance(m, Receipt):
                        ids.append(m.receipt_id)
                    elif isinstance(m, (Update, Barrier)) and m.receipt is not None:
                        ids.append(m.receipt)
                return ids

            self.assertEqual(
                receipt_ids(stream.messages),
                receipt_ids(result.stream.messages),
            )


if __name__ == "__main__":
    unittest.main()
