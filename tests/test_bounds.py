"""Tests for the pure bad-ratio bound maths and boundary behaviour."""

import unittest
from fractions import Fraction

from sli_adjudicator import (
    Bounds,
    VERDICT_ALLOW,
    VERDICT_REJECT,
    VERDICT_WAIT,
    compute_bounds,
    evaluate,
    to_fraction,
)


class ComputeBoundsTests(unittest.TestCase):
    def test_empty(self):
        b = compute_bounds(0, 0, 0)
        self.assertEqual(b.total, 0)
        self.assertEqual(b.lower, Fraction(0))
        self.assertEqual(b.upper, Fraction(0))

    def test_no_pending_bounds_collapse(self):
        b = compute_bounds(8, 2, 0)
        self.assertEqual(b.lower, Fraction(1, 5))
        self.assertEqual(b.upper, Fraction(1, 5))
        self.assertEqual(b.total, 10)

    def test_pending_widens_upper(self):
        b = compute_bounds(6, 1, 3)
        self.assertEqual(b.lower, Fraction(1, 10))   # pending as success: 1/10
        self.assertEqual(b.upper, Fraction(4, 10))    # pending as failure: 4/10
        self.assertEqual(b.total, 10)

    def test_all_pending(self):
        b = compute_bounds(0, 0, 4)
        self.assertEqual(b.lower, Fraction(0))
        self.assertEqual(b.upper, Fraction(1))
        self.assertEqual(b.total, 4)

    def test_negative_rejected(self):
        with self.assertRaises(ValueError):
            compute_bounds(-1, 0, 0)
        with self.assertRaises(ValueError):
            compute_bounds(0, True, 0)  # bool is not an int count

    def test_as_floats(self):
        b = compute_bounds(6, 1, 3)
        lo, hi = b.as_floats()
        self.assertAlmostEqual(lo, 0.1)
        self.assertAlmostEqual(hi, 0.4)


class EvaluateTests(unittest.TestCase):
    def test_no_samples_is_wait(self):
        verdict, b = evaluate(0, 0, 0, 0.5)
        self.assertEqual(verdict, VERDICT_WAIT)
        self.assertEqual(b.total, 0)

    def test_upper_equal_threshold_allows(self):
        # 1 failure / 10 == threshold -> upper == threshold -> allow.
        verdict, _ = evaluate(9, 1, 0, Fraction(1, 10))
        self.assertEqual(verdict, VERDICT_ALLOW)

    def test_upper_above_threshold_lower_at_or_below_is_wait(self):
        # lower == threshold (1/10), upper (2/10) > threshold -> wait.
        verdict, b = evaluate(8, 1, 1, Fraction(1, 10))
        self.assertEqual(verdict, VERDICT_WAIT)
        self.assertEqual(b.lower, Fraction(1, 10))
        self.assertEqual(b.upper, Fraction(2, 10))

    def test_lower_strictly_above_threshold_rejects(self):
        # lower = 2/10 > threshold 1/10 -> reject.
        verdict, _ = evaluate(7, 2, 1, Fraction(1, 10))
        self.assertEqual(verdict, VERDICT_REJECT)

    def test_lower_equal_threshold_is_wait_not_reject(self):
        # lower == threshold (1/10) exactly -> not strictly greater -> wait.
        verdict, _ = evaluate(9, 1, 0, Fraction(1, 10))
        self.assertEqual(verdict, VERDICT_ALLOW)
        # With a pending sample the lower is still 1/10 == threshold and the
        # upper (2/10) is above it -> wait, never reject.
        verdict, _ = evaluate(8, 1, 1, Fraction(1, 10))
        self.assertEqual(verdict, VERDICT_WAIT)

    def test_threshold_zero(self):
        # Zero tolerance: any failure -> lower > 0 -> reject.
        self.assertEqual(evaluate(10, 0, 0, 0)[0], VERDICT_ALLOW)
        self.assertEqual(evaluate(9, 1, 0, 0)[0], VERDICT_REJECT)
        # Pending only: lower 0, upper > 0 -> wait (straddles 0).
        self.assertEqual(evaluate(9, 0, 1, 0)[0], VERDICT_WAIT)

    def test_threshold_one(self):
        # Everything is within a 100% budget -> always allow when sampled.
        self.assertEqual(evaluate(0, 5, 0, 1)[0], VERDICT_ALLOW)
        self.assertEqual(evaluate(0, 0, 5, 1)[0], VERDICT_ALLOW)

    def test_float_vs_string_threshold_boundary(self):
        # 0.1 as a float is slightly > 1/10; as exact 1/10 the comparison is
        # exact.  A string threshold keeps the boundary crisp.
        self.assertEqual(evaluate(9, 1, 0, "0.1")[0], VERDICT_ALLOW)
        self.assertEqual(evaluate(9, 1, 0, Fraction(1, 10))[0], VERDICT_ALLOW)

    def test_invalid_threshold(self):
        for bad in (-0.1, 1.1):
            with self.assertRaises(ValueError):
                evaluate(1, 0, 0, bad)


class ToFractionTests(unittest.TestCase):
    def test_int(self):
        self.assertEqual(to_fraction(1), Fraction(1))

    def test_string_decimal_is_exact(self):
        self.assertEqual(to_fraction("0.1"), Fraction(1, 10))

    def test_string_ratio(self):
        self.assertEqual(to_fraction("3/25"), Fraction(3, 25))

    def test_fraction_passthrough(self):
        f = Fraction(2, 7)
        self.assertIs(to_fraction(f), f)

    def test_bool_rejected(self):
        with self.assertRaises(TypeError):
            to_fraction(True)

    def test_float_is_exact_binary(self):
        self.assertEqual(to_fraction(0.5), Fraction(1, 2))
        # Documents the float surprise: 0.1 != 1/10.
        self.assertNotEqual(to_fraction(0.1), Fraction(1, 10))


class BoundsDataclassTests(unittest.TestCase):
    def test_immutable(self):
        b = Bounds(Fraction(0), Fraction(1), 3)
        with self.assertRaises(Exception):
            b.total = 5  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
