"""Randomized optimality fuzz tests.

A deliberately simple, independently written brute-force dynamic program
enumerates every legal transition (all literal run lengths and every
authorized copy length) and therefore gives an unquestionable reference for
the minimum achievable cost.  The production encoder must match that cost on
every generated fixture, and every produced plan must (a) rebuild the target
byte for byte and (b) keep every copy fully inside one public interval.
"""

import random
import unittest

from diffencoder import CopyOp, decode, encode, plan_cost


def brute_force_optimal_cost(
    source: bytes,
    intervals,
    target: bytes,
    copy_cost: float,
    literal_head: float,
    literal_byte: float,
) -> float:
    """Reference DP in O(m^2 * n); only used on tiny fixtures."""
    m = len(target)
    n = len(source)
    infinity = float("inf")
    dp = [infinity] * (m + 1)
    dp[0] = 0

    for i in range(m):
        if dp[i] == infinity:
            continue
        # Every literal run length.
        for length in range(1, m - i + 1):
            dp[i + length] = min(
                dp[i + length],
                dp[i] + literal_head + length * literal_byte,
            )
        # Every authorized copy length, from every public source offset.
        for start, end in intervals:
            for offset in range(start, end):
                max_length = min(end - offset, m - i)
                length = 0
                while (
                    length < max_length
                    and source[offset + length] == target[i + length]
                ):
                    length += 1
                for used in range(1, length + 1):
                    dp[i + used] = min(dp[i + used], dp[i] + copy_cost)
        # Offsets outside intervals intentionally contribute nothing.
        assert n >= 0
    return dp[m]


class FuzzOptimalityTest(unittest.TestCase):
    def test_random_fixtures_match_brute_force(self):
        rng = random.Random(20261120)
        alphabet = b"ab"  # small alphabet creates plenty of overlapping matches

        for fixture in range(60):
            n = rng.randint(0, 12)
            m = rng.randint(0, 14)
            source = bytes(rng.choice(alphabet) for _ in range(n))
            target = bytes(rng.choice(alphabet) for _ in range(m))

            # Build 0-3 disjoint public intervals.
            intervals = []
            cursor = 0
            for _ in range(rng.randint(0, 3)):
                if cursor >= n:
                    break
                start = rng.randint(cursor, n - 1)
                end = rng.randint(start + 1, n)
                intervals.append((start, end))
                cursor = end

            copy_cost = rng.choice([0, 1, 2, 5, 10])
            literal_head = rng.choice([0, 1, 3, 8])
            literal_byte = rng.choice([0, 1, 2])

            with self.subTest(
                fixture=fixture,
                source=source,
                target=target,
                intervals=intervals,
                costs=(copy_cost, literal_head, literal_byte),
            ):
                plan = encode(
                    source,
                    intervals,
                    target,
                    copy_cost=copy_cost,
                    literal_head=literal_head,
                    literal_byte=literal_byte,
                )

                # 1. Byte-for-byte reconstruction through the validating decoder.
                self.assertEqual(decode(plan, source, intervals), target)

                # 2. Every copy is fully contained in a single public interval.
                for op in plan.ops:
                    if isinstance(op, CopyOp):
                        self.assertGreaterEqual(op.length, 1)
                        self.assertTrue(
                            any(
                                start <= op.offset
                                and op.offset + op.length <= end
                                for start, end in intervals
                            ),
                            f"unauthorized copy {op!r} with intervals {intervals}",
                        )
                        self.assertEqual(
                            source[op.offset : op.offset + op.length],
                            target[
                                _target_span_of(plan.ops, op)
                            ],
                        )

                # 3. Reported cost equals the independently computed op cost.
                self.assertEqual(
                    plan.cost,
                    plan_cost(plan.ops, copy_cost, literal_head, literal_byte),
                )

                # 4. Globally optimal: matches the brute-force reference.
                expected = brute_force_optimal_cost(
                    source,
                    intervals,
                    target,
                    copy_cost,
                    literal_head,
                    literal_byte,
                )
                self.assertEqual(plan.cost, expected)

    def test_known_boundary_fixture(self):
        # Target match would be 5 bytes; the public cap allows only 2, so the
        # parser must split copy + literal and remain optimal under two regimes.
        source = b"xxabcde"
        intervals = [(2, 4)]  # only "ab"
        target = b"abcde"

        for copy_cost, literal_head, literal_byte in (
            (1, 1, 1),
            (10, 1, 1),
            (1, 100, 1),
        ):
            plan = encode(
                source,
                intervals,
                target,
                copy_cost=copy_cost,
                literal_head=literal_head,
                literal_byte=literal_byte,
            )
            expected = brute_force_optimal_cost(
                source,
                intervals,
                target,
                copy_cost,
                literal_head,
                literal_byte,
            )
            self.assertEqual(plan.cost, expected)
            self.assertEqual(decode(plan, source, intervals), target)
            for op in plan.ops:
                if isinstance(op, CopyOp):
                    self.assertEqual((op.offset, op.length), (2, 2))


def _target_span_of(ops, wanted):
    """Return the target slice [start, start+length) occupied by an op."""
    cursor = 0
    for op in ops:
        length = op.length if isinstance(op, CopyOp) else len(op.data)
        if op is wanted:
            return slice(cursor, cursor + length)
        cursor += length
    raise AssertionError("op not found in plan")  # pragma: no cover


if __name__ == "__main__":
    unittest.main()
