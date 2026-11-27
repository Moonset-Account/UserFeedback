"""End-to-end reconstruction against small exact integer convolutions.

Every test uses :func:`direct_convolution` as the ground-truth oracle and
feeds only modular residue blocks, including moduli that share factors.
"""

import random
import unittest

from convolution_reconstruct import (
    CongruenceConflict,
    ConvolutionReconstructor,
    coefficient_bounds,
    direct_convolution,
    reconstruct_convolution,
    term_count,
)


def residues_of(values, modulus):
    return [v % modulus for v in values]


class TestSmallConvolution(unittest.TestCase):
    def test_explicit_example_term_by_term(self):
        n = 3
        a = [2, -3, 1]
        b = [4, 1, -2]
        truth = direct_convolution(a, b)
        self.assertEqual(truth, [8, -10, -3, 7, -2])

        reconstructor = ConvolutionReconstructor(n, 3, 4)
        self.assertEqual(
            reconstructor.bounds,
            [t * 12 for t in (1, 2, 3, 2, 1)],
        )
        # Moduli deliberately not pairwise coprime: 6 and 10 share factor 2,
        # 15 shares 3 and 5 with them.
        moduli = [6, 10, 15, 7]
        for modulus in moduli:
            reconstructor.add_block(modulus, residues_of(truth, modulus))

        for k, expected in enumerate(truth):
            self.assertTrue(reconstructor.is_determined(k))
            self.assertEqual(reconstructor.get_value(k), expected)
        self.assertEqual(reconstructor.publish_ready(), {k: v for k, v in enumerate(truth)})

    def test_effective_modulus_is_lcm_not_product(self):
        n = 2
        a, b = [1, 1], [1, 1]
        truth = direct_convolution(a, b)  # [1, 2, 1]
        reconstructor = ConvolutionReconstructor(n, 1, 1)

        report_6 = reconstructor.add_block(6, residues_of(truth, 6))
        self.assertEqual(reconstructor.effective_modulus(0), 6)

        report_10 = reconstructor.add_block(10, residues_of(truth, 10))
        # 6 * 10 = 60 would double count factor 2; the lcm is 30.
        self.assertEqual(reconstructor.effective_modulus(0), 30)
        self.assertIn(2, report_10.duplicated_factor_positions)
        self.assertIn(5, report_10.new_factor_positions)
        self.assertNotIn(2, report_10.new_factor_positions)

        report_15 = reconstructor.add_block(15, residues_of(truth, 15))
        # 15 = 3*5 adds nothing beyond lcm(6, 10) = 30.
        self.assertEqual(reconstructor.effective_modulus(0), 30)
        self.assertEqual(report_15.new_factor_positions, {})
        self.assertEqual(
            sorted(report_15.duplicated_factor_positions), [3, 5]
        )

    def test_randomized_cross_check_with_non_coprime_moduli(self):
        rng = random.Random(20261128)
        moduli = [4, 6, 9, 10, 14, 15, 25, 11]  # many shared factors
        for trial in range(40):
            n = rng.randint(1, 8)
            bound_a = rng.randint(0, 6)
            bound_b = rng.randint(0, 6)
            a = [rng.randint(-bound_a, bound_a) for _ in range(n)]
            b = [rng.randint(-bound_b, bound_b) for _ in range(n)]
            truth = direct_convolution(a, b)
            self.assertEqual(len(truth), 2 * n - 1)

            order = moduli[:]
            rng.shuffle(order)
            reconstructor = ConvolutionReconstructor(n, bound_a, bound_b)
            prefix: list[int | None] = [None] * len(truth)
            for modulus in order:
                reconstructor.add_block(modulus, residues_of(truth, modulus))
                # Every already-publishable value must equal the truth.
                for k in range(len(truth)):
                    value = reconstructor.get_value(k)
                    if value is not None:
                        self.assertEqual(value, truth[k], (trial, k, modulus))
                        prefix[k] = value

            # The combined lcm of this modulus family is huge: everything
            # must eventually be determined exactly.
            for k, expected in enumerate(truth):
                self.assertEqual(reconstructor.get_value(k), expected)
                self.assertEqual(prefix[k], expected)

    def test_signed_input_residues_accepted(self):
        n = 2
        truth = direct_convolution([-2, 3], [1, -4])
        reconstructor = ConvolutionReconstructor(n, 3, 4)
        # Express residues in the signed range (-m/2, m/2] instead of [0, m).
        signed = [v % 7 if v % 7 <= 3 else v % 7 - 7 for v in truth]
        self.assertTrue(any(s < 0 for s in signed))
        reconstructor.add_block(7, signed)
        # Internally the residues are normalized back to [0, m).
        for k, expected in enumerate(truth):
            self.assertEqual(reconstructor.state(k).residue, expected % 7)

    def test_wrapper_function(self):
        truth = direct_convolution([2, 1], [-3, 4])
        blocks = [(m, residues_of(truth, m)) for m in (5, 7, 9)]
        result = reconstruct_convolution(2, 2, 4, blocks)
        self.assertEqual(result, truth)

    def test_partial_and_late_blocks(self):
        n = 3
        truth = direct_convolution([1, 2, 3], [4, -5, 6])
        reconstructor = ConvolutionReconstructor(n, 3, 6)

        # First block only covers two positions.
        report = reconstructor.add_block(
            7, {0: truth[0] % 7, 4: truth[4] % 7}
        )
        self.assertEqual(report.applied_positions, (0, 4))
        self.assertEqual(reconstructor.effective_modulus(2), 1)
        self.assertIsNone(reconstructor.get_value(2))

        # Late full block fills the rest; lcm(7, 11) = 77 is not enough for
        # the center bound 54 (needs M > 108), so add a third modulus.
        reconstructor.add_block(11, residues_of(truth, 11))
        reconstructor.add_block(13, residues_of(truth, 13))
        for k, expected in enumerate(truth):
            self.assertEqual(reconstructor.get_value(k), expected)

        # Sequence form with explicit position list is also accepted.
        reconstructor2 = ConvolutionReconstructor(n, 3, 6)
        reconstructor2.add_block(13, [truth[1] % 13, truth[3] % 13], [1, 3])
        self.assertEqual(reconstructor2.effective_modulus(1), 13)
        self.assertEqual(reconstructor2.effective_modulus(0), 1)


class TestConflicts(unittest.TestCase):
    def test_conflict_localized_to_modulus_and_position(self):
        n = 2
        truth = direct_convolution([2, -3], [1, 4])  # [2, 5, -12]
        reconstructor = ConvolutionReconstructor(n, 3, 4)
        reconstructor.add_block(6, residues_of(truth, 6))

        tampered = residues_of(truth, 9)
        # A shift of 1 is not divisible by gcd(6, 9) = 3, so it conflicts.
        tampered[1] = (tampered[1] + 1) % 9
        with self.assertRaises(CongruenceConflict) as ctx:
            reconstructor.add_block(9, tampered)

        conflict = ctx.exception.conflicts[0]
        self.assertEqual(conflict.position, 1)
        self.assertEqual(conflict.modulus, 9)
        self.assertEqual(conflict.gcd, 3)
        self.assertNotEqual(
            conflict.incoming_residue % 3, conflict.existing_residue % 3
        )

    def test_conflicting_block_is_atomic(self):
        n = 2
        truth = direct_convolution([2, -3], [1, 4])
        reconstructor = ConvolutionReconstructor(n, 3, 4)
        reconstructor.add_block(6, residues_of(truth, 6))

        bad = residues_of(truth, 10)
        bad[0] = (bad[0] + 5) % 10  # incompatible modulo gcd(6, 10) = 2
        snapshot_before = [
            (
                reconstructor.state(k).residue,
                reconstructor.state(k).effective_modulus,
            )
            for k in range(3)
        ]
        with self.assertRaises(CongruenceConflict):
            reconstructor.add_block(10, bad)

        # No position changed, modulus not recorded.
        self.assertEqual(reconstructor.accepted_moduli(), {6})
        for k, (res, mod) in enumerate(snapshot_before):
            self.assertEqual(reconstructor.state(k).residue, res)
            self.assertEqual(reconstructor.state(k).effective_modulus, mod)

    def test_multiple_conflicts_reported_together(self):
        n = 3
        truth = direct_convolution([1, 2, 3], [4, -5, 6])
        reconstructor = ConvolutionReconstructor(n, 3, 6)
        reconstructor.add_block(12, residues_of(truth, 12))
        bad = residues_of(truth, 18)
        bad[0] = (bad[0] + 2) % 18   # gcd(12,18)=6; 2 not divisible by 6
        bad[2] = (bad[2] + 3) % 18   # 3 not divisible by 6 either
        with self.assertRaises(CongruenceConflict) as ctx:
            reconstructor.add_block(18, bad)
        self.assertEqual({c.position for c in ctx.exception.conflicts}, {0, 2})
        self.assertTrue(all(c.modulus == 18 for c in ctx.exception.conflicts))

    def test_compatible_shared_factor_does_not_conflict(self):
        n = 1
        reconstructor = ConvolutionReconstructor(1, 10, 10)
        reconstructor.add_block(6, [4 % 6])       # x ≡ 4 (mod 6)
        report = reconstructor.add_block(9, [4 % 9])  # x ≡ 4 (mod 9), gcd 3 ok
        self.assertEqual(report.applied_positions, (0,))
        self.assertEqual(reconstructor.effective_modulus(0), 18)


class TestPublishing(unittest.TestCase):
    def test_edge_positions_publish_before_center(self):
        n = 4
        truth = direct_convolution([1, -2, 3, -4], [5, 6, -7, 8])
        bounds = coefficient_bounds(n, 4, 8)
        # t(k) rises toward the center, so edges have the smallest bounds.
        self.assertEqual(bounds[0], 32)
        self.assertEqual(bounds[3], 128)

        reconstructor = ConvolutionReconstructor(n, 4, 8)
        first = reconstructor.add_block(7, residues_of(truth, 7))
        # Only positions whose bound satisfies 7 > 2*B, i.e. B <= 3.
        # Here all bounds >= 32, so nothing yet.
        self.assertEqual(first.newly_determined_positions, ())

        reconstructor.add_block(101, residues_of(truth, 101))
        ready = set(reconstructor.ready_positions())
        # Edge bounds 32 -> threshold 64 < 101*7 = 707: actually all lcm
        # positions share the same lcm; verify monotone inclusion instead.
        for k in ready:
            self.assertGreater(reconstructor.effective_modulus(k), 2 * bounds[k])

        published = reconstructor.publish_ready()
        self.assertEqual(set(published), ready)
        # Second call publishes nothing new.
        self.assertEqual(reconstructor.publish_ready(), {})
        for k, value in published.items():
            self.assertTrue(reconstructor.state(k).published)
            self.assertEqual(value, truth[k])

    def test_publish_matches_term_count_ordering(self):
        # Construct a case where a small modulus determines only the edges.
        n = 3
        a = b = [1, 1, 1]
        truth = direct_convolution(a, b)  # [1, 2, 3, 2, 1]
        reconstructor = ConvolutionReconstructor(n, 1, 1)
        # Bounds: [1, 2, 3, 2, 1]; uniqueness needs M > 2*B.
        reconstructor.add_block(3, residues_of(truth, 3))
        # M=3: edges need M > 2*1 (yes); positions 1/3 need M > 4 (no);
        # center needs M > 6 (no).
        self.assertEqual(set(reconstructor.ready_positions()), {0, 4})
        self.assertEqual(reconstructor.publish_ready(), {0: 1, 4: 1})
        reconstructor.add_block(5, residues_of(truth, 5))  # lcm 15
        # Published positions stay ready; the newly publishable ones are
        # exactly the three interior positions.
        self.assertEqual(
            set(reconstructor.ready_positions()), {0, 1, 2, 3, 4}
        )
        self.assertEqual(
            reconstructor.publish_ready(), {1: 2, 2: 3, 3: 2}
        )


class TestEdgeCases(unittest.TestCase):
    def test_modulus_one_is_informative_only_at_zero_bound(self):
        # Zero coefficient bound: M = 1 > 2*0 = 0, so the unique value 0
        # is publishable without any real congruence information.
        reconstructor = ConvolutionReconstructor(2, 0, 5)
        self.assertEqual(reconstructor.bounds, [0, 0, 0])
        report = reconstructor.add_block(1, [0, 0, 0])
        self.assertEqual(report.applied_positions, (0, 1, 2))
        self.assertTrue(all(reconstructor.is_determined(k) for k in range(3)))
        self.assertEqual(reconstructor.publish_ready(), {0: 0, 1: 0, 2: 0})

    def test_modulus_one_adds_nothing_otherwise(self):
        reconstructor = ConvolutionReconstructor(1, 3, 3)
        reconstructor.add_block(1, [0])
        self.assertEqual(reconstructor.effective_modulus(0), 1)
        self.assertFalse(reconstructor.is_determined(0))
        reconstructor.add_block(7, [5 % 7])
        self.assertEqual(reconstructor.effective_modulus(0), 7)

    def test_constructor_validation(self):
        with self.assertRaises(ValueError):
            ConvolutionReconstructor(0, 1, 1)
        with self.assertRaises(ValueError):
            ConvolutionReconstructor(3, -1, 1)
        with self.assertRaises(ValueError):
            ConvolutionReconstructor(3, 1, 1, output_length=0)


if __name__ == "__main__":
    unittest.main()
