"""Coefficients beyond 64-bit range and exact upper/lower-bound behavior."""

import unittest

from convolution_reconstruct import (
    ConvolutionReconstructor,
    direct_convolution,
    factorize,
)

LARGE_PRIMES = (1_000_000_007, 1_000_000_009, 998_244_353)
SIGNED_64_MAX = 2**63 - 1


def make_large_sequence(seed: int, n: int, scale: int) -> list[int]:
    """Deterministic pseudo-random sequence in [-scale, scale], mixed signs."""
    state = seed
    out: list[int] = []
    for _ in range(n):
        # xorshift32, no external dependency.
        state ^= (state << 13) & 0xFFFFFFFF
        state ^= state >> 17
        state ^= (state << 5) & 0xFFFFFFFF
        state &= 0xFFFFFFFF
        magnitude = state % (scale + 1)
        out.append(magnitude if state % 2 == 0 else -magnitude)
    return out


class TestBigIntegerCoefficients(unittest.TestCase):
    def setUp(self):
        self.n = 16
        self.scale = 10**12
        self.a = make_large_sequence(0x1234ABCD, self.n, self.scale)
        self.b = make_large_sequence(0xCAFEF00D, self.n, self.scale)
        self.truth = direct_convolution(self.a, self.b)

    def test_coefficients_exceed_64_bits(self):
        self.assertEqual(len(self.truth), 2 * self.n - 1)
        self.assertTrue(any(abs(v) > SIGNED_64_MAX for v in self.truth))
        # Loose theoretical max: t(k) * scale^2, center has n terms.
        theoretical_max = self.n * self.scale * self.scale
        self.assertLessEqual(max(abs(v) for v in self.truth), theoretical_max)

    def test_reconstruction_with_large_moduli(self):
        # The large primes are pairwise coprime (distinct primes).
        for p in LARGE_PRIMES:
            self.assertEqual(factorize(p), {p: 1})
        self.assertEqual(len(set(LARGE_PRIMES)), len(LARGE_PRIMES))

        reconstructor = ConvolutionReconstructor(self.n, self.scale, self.scale)
        full_lcm = 1
        for p in LARGE_PRIMES:
            reconstructor.add_block(p, [v % p for v in self.truth])
            full_lcm *= p

        for k, expected in enumerate(self.truth):
            state = reconstructor.state(k)
            self.assertEqual(state.effective_modulus, full_lcm)
            self.assertGreater(state.effective_modulus, 2 * state.bound)
            self.assertEqual(state.value(), expected)
            # Python ints: no wraparound, negative values preserved.
            self.assertIsInstance(state.value(), int)

        self.assertEqual(reconstructor.snapshot(), {k: v for k, v in enumerate(self.truth)})

    def test_partial_information_insufficient(self):
        reconstructor = ConvolutionReconstructor(self.n, self.scale, self.scale)
        reconstructor.add_block(LARGE_PRIMES[0], [v % LARGE_PRIMES[0] for v in self.truth])
        # One ~1e9 modulus versus bounds ~1e24: nothing is publishable.
        self.assertEqual(reconstructor.ready_positions(), [])
        self.assertTrue(all(v is None for v in reconstructor.snapshot().values()))

        reconstructor.add_block(LARGE_PRIMES[1], [v % LARGE_PRIMES[1] for v in self.truth])
        # Two primes give ~1e18, still below 2 * (16 * 1e24) = 3.2e25.
        self.assertEqual(reconstructor.ready_positions(), [])

        reconstructor.add_block(LARGE_PRIMES[2], [v % LARGE_PRIMES[2] for v in self.truth])
        self.assertEqual(len(reconstructor.ready_positions()), 2 * self.n - 1)
        self.assertEqual(
            [reconstructor.get_value(k) for k in range(2 * self.n - 1)],
            self.truth,
        )

    def test_declared_bounds_hold_and_are_tight_in_shape(self):
        reconstructor = ConvolutionReconstructor(self.n, self.scale, self.scale)
        for k, value in enumerate(self.truth):
            self.assertLessEqual(abs(value), reconstructor.bounds[k])
        # Bounds follow the term-count triangle.
        self.assertEqual(
            reconstructor.bounds,
            [min(k + 1, 2 * self.n - 1 - k) * self.scale**2
             for k in range(2 * self.n - 1)],
        )


class TestUniquenessThreshold(unittest.TestCase):
    """The M > 2B rule: boundary moduli must NOT trigger early publishing."""

    def test_modulus_equal_to_two_b_is_ambiguous(self):
        # n = 1, a = [B], b = [1] -> single coefficient x with |x| <= B.
        B = 5
        reconstructor = ConvolutionReconstructor(1, B, 1)
        self.assertEqual(reconstructor.bounds, [B])

        # x = B = 5, residue modulo 2B = 10 is 5.  Both 5 and -5 share it.
        reconstructor.add_block(2 * B, [B % (2 * B)])
        self.assertEqual(reconstructor.effective_modulus(0), 2 * B)
        self.assertFalse(reconstructor.is_determined(0))
        self.assertIsNone(reconstructor.get_value(0))

        # One extra unit of modulus breaks the tie.
        reconstructor.add_block(2 * B + 1, [B % (2 * B + 1)])
        self.assertTrue(reconstructor.is_determined(0))
        self.assertEqual(reconstructor.get_value(0), B)

    def test_ambiguous_pair_demonstrated(self):
        # Within [-B, B], two distinct integers congruent mod M exist iff
        # M <= 2B: 0 and M (when M <= B), or B and B-M for general M.
        B, M = 7, 4
        x1, x2 = 3, 3 - M  # 3 and -1, both in [-7, 7]
        self.assertTrue(-B <= x2 < x1 <= B)
        self.assertEqual(x1 % M, x2 % M)

        reconstructor = ConvolutionReconstructor(1, B, 1)
        reconstructor.add_block(M, [x1 % M])
        self.assertIsNone(reconstructor.get_value(0))

    def test_negative_coefficient_published(self):
        # Need lcm > 2*100 = 200 so that -91 lies in the signed lift range.
        reconstructor = ConvolutionReconstructor(1, 10, 10)
        reconstructor.add_block(7, [(-91) % 7])
        reconstructor.add_block(29, [(-91) % 29])  # lcm 203 > 200
        self.assertEqual(reconstructor.effective_modulus(0), 203)
        self.assertTrue(reconstructor.is_determined(0))
        self.assertEqual(reconstructor.get_value(0), -91)


if __name__ == "__main__":
    unittest.main()
