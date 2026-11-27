"""Interleaved / out-of-order / duplicated block delivery scenarios."""

import random
import unittest

from convolution_reconstruct import (
    CongruenceConflict,
    ConvolutionReconstructor,
    direct_convolution,
)


class TestInterleaving(unittest.TestCase):
    def test_blocks_arrive_out_of_order_and_split(self):
        rng = random.Random(77)
        n = 5
        a = [rng.randint(-4, 4) for _ in range(n)]
        b = [rng.randint(-4, 4) for _ in range(n)]
        truth = direct_convolution(a, b)

        reconstructor = ConvolutionReconstructor(n, 4, 4)
        moduli = [6, 10, 15, 7, 11]
        # Split each modulus block into two arbitrary position subsets.
        positions = list(range(2 * n - 1))
        for modulus in moduli:
            rng.shuffle(positions)
            left, right = positions[:6], positions[6:]
            reconstructor.add_block(
                modulus, {k: truth[k] % modulus for k in left}
            )
            reconstructor.add_block(
                modulus, {k: truth[k] % modulus for k in right}
            )

        for k, expected in enumerate(truth):
            self.assertEqual(reconstructor.get_value(k), expected)
            # Same modulus may appear twice; it is recorded once as accepted.
        self.assertEqual(reconstructor.accepted_moduli(), set(moduli))

    def test_repeated_identical_block_adds_no_information(self):
        n = 2
        truth = direct_convolution([3, -2], [1, 5])
        reconstructor = ConvolutionReconstructor(n, 3, 5)
        first = reconstructor.add_block(12, [v % 12 for v in truth])
        self.assertNotEqual(first.new_factor_positions, {})

        second = reconstructor.add_block(12, [v % 12 for v in truth])
        self.assertEqual(second.new_factor_positions, {})
        self.assertEqual(
            sorted(second.duplicated_factor_positions), [2, 3]
        )
        self.assertEqual(reconstructor.effective_modulus(0), 12)
        self.assertEqual(
            reconstructor.state(0).source_moduli, [12, 12]
        )

    def test_late_block_conflict_after_other_progress(self):
        n = 3
        truth = direct_convolution([2, 1, -3], [4, -2, 1])
        reconstructor = ConvolutionReconstructor(n, 3, 4)

        reconstructor.add_block(8, [v % 8 for v in truth])
        reconstructor.add_block(9, [v % 9 for v in truth])  # lcm 72
        healthy_lcm = reconstructor.effective_modulus(2)

        # A late worker reports a wrong residue at position 2 modulo 12.
        # gcd(lcm(8,9)=72, 12) = 12, so the shift must not be divisible by 12.
        late = [v % 12 for v in truth]
        late[2] = (late[2] + 1) % 12
        with self.assertRaises(CongruenceConflict) as ctx:
            reconstructor.add_block(12, late)
        self.assertEqual(ctx.exception.conflicts[0].position, 2)
        self.assertEqual(ctx.exception.conflicts[0].modulus, 12)

        # State remains exactly as before the rejected late block.
        self.assertEqual(reconstructor.effective_modulus(2), healthy_lcm)
        self.assertEqual(reconstructor.accepted_moduli(), {8, 9})

        # Re-submitting the corrected block succeeds (lcm 72,12 stays 72);
        # a fresh coprime modulus then makes every position publishable.
        good = [v % 12 for v in truth]
        report = reconstructor.add_block(12, good)
        self.assertEqual(report.modulus, 12)
        reconstructor.add_block(17, [v % 17 for v in truth])
        for k, expected in enumerate(truth):
            self.assertEqual(reconstructor.get_value(k), expected)

    def test_information_growth_uses_lcm_under_interleaving(self):
        n = 1
        reconstructor = ConvolutionReconstructor(1, 100, 100)
        # Chain of overlapping moduli: 12 -> 18 -> 30 -> 45
        # lcm progression: 12, 36, 180, 180 (45 adds nothing).
        x = -137
        reconstructor.add_block(12, [x % 12])
        self.assertEqual(reconstructor.effective_modulus(0), 12)
        reconstructor.add_block(18, [x % 18])
        self.assertEqual(reconstructor.effective_modulus(0), 36)
        report_30 = reconstructor.add_block(30, [x % 30])
        self.assertEqual(reconstructor.effective_modulus(0), 180)
        # 30 = 2*3*5: factor 2 (2^1 <= 2^2) and 3 (3^1 <= 3^2) duplicate,
        # only prime 5 is genuinely new.
        self.assertEqual(list(report_30.new_factor_positions), [5])
        self.assertEqual(
            sorted(report_30.duplicated_factor_positions), [2, 3]
        )
        report_45 = reconstructor.add_block(45, [x % 45])
        self.assertEqual(reconstructor.effective_modulus(0), 180)
        self.assertEqual(report_45.new_factor_positions, {})

        # 180 <= 2*10000 = 20000: still ambiguous, nothing published.
        self.assertFalse(reconstructor.is_determined(0))
        self.assertIsNone(reconstructor.get_value(0))

    def test_factor_owners_track_strongest_supplier(self):
        n = 1
        reconstructor = ConvolutionReconstructor(1, 10, 10)
        reconstructor.add_block(6, [1 % 6])    # 2^1 * 3
        reconstructor.add_block(4, [1 % 4])    # 2^2 upgrades factor 2
        reconstructor.add_block(9, [1 % 9])    # 3^2 upgrades factor 3

        state = reconstructor.state(0)
        self.assertEqual(state.factors, {2: 2, 3: 2})
        self.assertEqual(state.factor_owners, {2: 4, 3: 9})
        self.assertEqual(state.effective_modulus, 36)

        report = reconstructor.factor_report()
        self.assertEqual(report[2]["owner_modulus"], 4)
        self.assertEqual(report[3]["owner_modulus"], 9)

    def test_invalid_block_shapes_rejected(self):
        reconstructor = ConvolutionReconstructor(3, 1, 1)  # output length 5
        with self.assertRaises(ValueError):
            reconstructor.add_block(3, [0, 1, 2])  # too short
        with self.assertRaises(ValueError):
            reconstructor.add_block(3, [0, 1], [0])  # mismatched positions
        with self.assertRaises(IndexError):
            reconstructor.add_block(3, {9: 0})
        with self.assertRaises(ValueError):
            reconstructor.add_block(0, [0] * 5)
        with self.assertRaises(TypeError):
            reconstructor.add_block(3, {0: 1.5})
        with self.assertRaises(ValueError):
            ConvolutionReconstructor(3, 1, 1, output_length=99)

    def test_truncated_output_length(self):
        n = 3
        truth = direct_convolution([1, 2, 3], [4, 5, 6])
        reconstructor = ConvolutionReconstructor(n, 3, 6, output_length=3)
        # Per-product bound = 18, term counts 1, 2, 3.
        self.assertEqual(reconstructor.bounds, [18, 36, 54])
        # lcm(13, 17, 19) = 4199 > 2 * 54 = 108.
        for modulus in (13, 17, 19):
            reconstructor.add_block(
                modulus, [v % modulus for v in truth[:3]]
            )
        self.assertEqual(
            [reconstructor.get_value(k) for k in range(3)], truth[:3]
        )
        with self.assertRaises(IndexError):
            reconstructor.state(5)


if __name__ == "__main__":
    unittest.main()
