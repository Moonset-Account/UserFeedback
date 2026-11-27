"""Unit tests for the number-theoretic primitives and bound geometry."""

import unittest

from convolution_reconstruct import (
    coefficient_bound,
    coefficient_bounds,
    factorize,
    merge_congruence,
    prime_power_lcm,
    signed_residue,
    term_count,
)


class TestTermCountAndBounds(unittest.TestCase):
    def test_term_count_triangle(self):
        n = 4
        expected = [1, 2, 3, 4, 3, 2, 1]
        self.assertEqual([term_count(k, n) for k in range(2 * n - 1)], expected)

    def test_term_count_outside_range(self):
        self.assertEqual(term_count(-1, 3), 0)
        self.assertEqual(term_count(5, 3), 0)

    def test_coefficient_bounds_product(self):
        n = 3
        bounds = coefficient_bounds(n, 2, 5)
        # t(k) = 1, 2, 3, 2, 1 and per-product bound = 10
        self.assertEqual(bounds, [10, 20, 30, 20, 10])
        self.assertEqual(coefficient_bound(2, n, 2, 5), 30)

    def test_zero_bounds(self):
        self.assertEqual(coefficient_bounds(3, 0, 99), [0, 0, 0, 0, 0])

    def test_invalid_inputs(self):
        with self.assertRaises(ValueError):
            term_count(0, 0)
        with self.assertRaises(ValueError):
            coefficient_bounds(0, 1, 1)
        with self.assertRaises(ValueError):
            coefficient_bounds(3, -1, 1)
        with self.assertRaises(IndexError):
            coefficient_bound(99, 3, 1, 1)


class TestFactorize(unittest.TestCase):
    def test_known_factorizations(self):
        self.assertEqual(factorize(2), {2: 1})
        self.assertEqual(factorize(12), {2: 2, 3: 1})
        self.assertEqual(factorize(360), {2: 3, 3: 2, 5: 1})
        self.assertEqual(factorize(1_000_000_007), {1_000_000_007: 1})

    def test_roundtrip(self):
        for m in (2, 6, 12, 360, 97, 2**10 * 3**5):
            self.assertEqual(prime_power_lcm(factorize(m)), m)

    def test_invalid(self):
        with self.assertRaises(ValueError):
            factorize(1)
        with self.assertRaises(ValueError):
            factorize(0)


class TestGeneralizedCRT(unittest.TestCase):
    def test_coprime_merge(self):
        r, m = merge_congruence(2, 3, 3, 5)
        self.assertEqual(m, 15)
        self.assertEqual(r % 3, 2)
        self.assertEqual(r % 5, 3)

    def test_non_coprime_compatible(self):
        # x ≡ 1 (mod 6), x ≡ 4 (mod 9): gcd = 3, 1 ≡ 4 (mod 3)
        r, m = merge_congruence(1, 6, 4, 9)
        self.assertEqual(m, 18)
        self.assertEqual(r % 6, 1)
        self.assertEqual(r % 9, 4)

    def test_non_coprime_conflict(self):
        # x ≡ 1 (mod 6), x ≡ 2 (mod 9): 1 != 2 (mod 3)
        self.assertIsNone(merge_congruence(1, 6, 2, 9))

    def test_divisibility_merge_keeps_larger_modulus(self):
        r, m = merge_congruence(3, 12, 7, 4)
        self.assertEqual(m, 12)
        self.assertEqual(r, 3)
        self.assertEqual(3 % 4, 7 % 4)

    def test_identity_modulus_one(self):
        self.assertEqual(merge_congruence(0, 1, 5, 7), (5, 7))
        self.assertEqual(merge_congruence(5, 7, 0, 1), (5, 7))

    def test_chained_merge_matches_direct_crt(self):
        # Merge 4, 6, 5, 9 step by step; result must equal lcm residue.
        r, m = 0, 1
        target = 37
        for modulus in (4, 6, 5, 9):
            r, m = merge_congruence(r, m, target % modulus, modulus)
        self.assertEqual(m, 180)
        self.assertEqual(r, target % 180)


class TestSignedResidue(unittest.TestCase):
    def test_range(self):
        self.assertEqual(signed_residue(3, 7), 3)
        self.assertEqual(signed_residue(5, 7), -2)
        self.assertEqual(signed_residue(0, 5), 0)
        # even modulus: half stays positive
        self.assertEqual(signed_residue(3, 6), 3)
        self.assertEqual(signed_residue(4, 6), -2)

    def test_negative_input_normalized(self):
        self.assertEqual(signed_residue(-2, 7), signed_residue(5, 7))


if __name__ == "__main__":
    unittest.main()
