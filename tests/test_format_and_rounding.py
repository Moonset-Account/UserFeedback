"""Tests for the small binary format and the stateless rounding kernel."""

from __future__ import annotations

import unittest
from fractions import Fraction as F

from fpiso.errors import FormatError
from fpiso.flags import Flag
from fpiso.format import SMALL, Format, RoundingMode, coerce_rounding
from fpiso.rounding import round_exact
from fpiso.value import FPValue


def small(name: str = "small", **kwargs) -> Format:
    params = dict(precision=4, emin=-2, emax=3)
    params.update(kwargs)
    return Format(name=name, **params)


class FormatDescriptionTests(unittest.TestCase):
    def test_small_format_derived_quantities(self) -> None:
        self.assertEqual(SMALL.precision, 4)
        self.assertEqual(SMALL.step_min, -5)
        self.assertEqual(SMALL.step_max, 0)
        self.assertEqual(SMALL.min_normal, F(1, 4))
        self.assertEqual(SMALL.min_subnormal, F(1, 32))
        self.assertEqual(SMALL.max_finite, F(15))

    def test_invalid_formats_rejected(self) -> None:
        with self.assertRaises(FormatError):
            Format("x", 0, -1, 1)
        with self.assertRaises(FormatError):
            Format("x", 4, 1, -1)
        with self.assertRaises(FormatError):
            Format("", 4, -1, 1)
        with self.assertRaises(FormatError):
            Format("x", 4, -1, 1, has_infinity="yes")

    def test_unknown_rounding_mode_rejected(self) -> None:
        with self.assertRaises(FormatError):
            coerce_rounding("nearest")


class RepresentabilityTests(unittest.TestCase):
    def test_grid_membership(self) -> None:
        for value in (F(0), F(1, 32), F(1, 16), F(3, 32), F(1, 4), F(5, 4), F(15)):
            self.assertTrue(SMALL.is_representable(value), value)
        for value in (F(1, 64), F(17, 16), F(16), F(-1, 64), F(155, 10)):
            self.assertFalse(SMALL.is_representable(value), value)

    def test_neighbors_of_inexact_value(self) -> None:
        below, above = SMALL.neighbors(F(17, 16))
        self.assertEqual((below, above), (F(1), F(9, 8)))

    def test_successor_and_predecessor(self) -> None:
        self.assertEqual(SMALL.successor(F(1)), F(9, 8))
        self.assertEqual(SMALL.predecessor(F(1)), F(15, 16))
        self.assertEqual(SMALL.successor(F(1, 4)), F(9, 32))
        self.assertEqual(SMALL.predecessor(F(1, 32)), F(0))
        with self.assertRaises(FormatError):
            SMALL.successor(F(15))

    def test_predecessor_of_zero_crosses_sign(self) -> None:
        self.assertEqual(SMALL.predecessor(F(0)), F(-1, 32))

    def test_neighbors_below_smallest_subnormal(self) -> None:
        below, above = SMALL.neighbors(F(1, 1000))
        self.assertEqual(below, F(0))
        self.assertEqual(above, F(1, 32))


class MidpointRoundingTests(unittest.TestCase):
    """17/16 is exactly halfway between 1 and 9/8 in the small format."""

    midpoint = F(17, 16)

    def test_ties_to_even_goes_to_even_neighbour(self) -> None:
        self.assertEqual(round_exact(self.midpoint, SMALL, RoundingMode.RNE).value, F(1))
        # 21/16 is exactly halfway between 5/4 (significand 10) and 11/8
        # (significand 11), so ties-to-even selects 5/4.
        self.assertFalse(SMALL.is_representable(F(21, 16)))
        self.assertEqual(
            round_exact(F(21, 16), SMALL, RoundingMode.RNE).value, F(5, 4)
        )
        self.assertEqual(
            round_exact(F(21, 16), SMALL, RoundingMode.RNA).value, F(11, 8)
        )

    def test_ties_away_from_zero(self) -> None:
        self.assertEqual(
            round_exact(self.midpoint, SMALL, RoundingMode.RNA).value, F(9, 8)
        )
        self.assertEqual(
            round_exact(-self.midpoint, SMALL, RoundingMode.RNA).value, F(-9, 8)
        )

    def test_directed_modes(self) -> None:
        self.assertEqual(round_exact(self.midpoint, SMALL, RoundingMode.RZ).value, F(1))
        self.assertEqual(round_exact(self.midpoint, SMALL, RoundingMode.RD).value, F(1))
        self.assertEqual(round_exact(self.midpoint, SMALL, RoundingMode.RU).value, F(9, 8))

    def test_directed_modes_negative(self) -> None:
        self.assertEqual(round_exact(-self.midpoint, SMALL, RoundingMode.RZ).value, F(-1))
        self.assertEqual(round_exact(-self.midpoint, SMALL, RoundingMode.RD).value, F(-9, 8))
        self.assertEqual(round_exact(-self.midpoint, SMALL, RoundingMode.RU).value, F(-1))

    def test_rounding_flag_is_inexact_only(self) -> None:
        for mode in RoundingMode:
            result = round_exact(self.midpoint, SMALL, mode)
            self.assertEqual(result.flags, frozenset({Flag.INEXACT}), mode)

    def test_exact_values_raise_no_flags(self) -> None:
        for value in (F(1), F(9, 8), F(15), F(1, 32)):
            for mode in RoundingMode:
                result = round_exact(value, SMALL, mode)
                self.assertEqual(result.flags, frozenset(), (value, mode))
                self.assertEqual(result.value, value)

    def test_zero_is_signed_and_quiet(self) -> None:
        result = round_exact(F(0), SMALL, RoundingMode.RNE, zero_sign=-1)
        self.assertEqual(result.value, FPValue.zero(-1))
        self.assertEqual(str(result.value), "-0")
        self.assertEqual(result.flags, frozenset())


class OverflowTests(unittest.TestCase):
    def test_nearest_rounds_to_infinity(self) -> None:
        for mode in (RoundingMode.RNE, RoundingMode.RNA, RoundingMode.RU):
            result = round_exact(F(16), SMALL, mode)
            self.assertTrue(result.value.is_infinite, mode)
            self.assertEqual(result.value.sign, 1)
            self.assertEqual(result.flags, frozenset({Flag.OVERFLOW, Flag.INEXACT}))

    def test_toward_zero_saturates_at_max(self) -> None:
        result = round_exact(F(16), SMALL, RoundingMode.RZ)
        self.assertEqual(result.value, F(15))
        self.assertEqual(result.flags, frozenset({Flag.OVERFLOW, Flag.INEXACT}))

    def test_negative_overflow_direction(self) -> None:
        self.assertIn(Flag.OVERFLOW, round_exact(F(-16), SMALL, RoundingMode.RD).flags)
        self.assertTrue(round_exact(F(-16), SMALL, RoundingMode.RD).value.is_infinite)
        self.assertEqual(
            round_exact(F(-16), SMALL, RoundingMode.RU).value, F(-15)
        )

    def test_value_below_max_but_rounding_over(self) -> None:
        # 31/2 = 15.5 rounds to 16 (above max finite 15) under RNE.
        result = round_exact(F(31, 2), SMALL, RoundingMode.RNE)
        self.assertTrue(result.value.is_infinite)
        self.assertIn(Flag.OVERFLOW, result.flags)
        # Under RZ it truncates back to 15 without the overflow event.
        result = round_exact(F(31, 2), SMALL, RoundingMode.RZ)
        self.assertEqual(result.value, F(15))
        self.assertEqual(result.flags, frozenset({Flag.INEXACT}))

    def test_finite_only_format_saturates(self) -> None:
        fmt = small(has_infinity=False)
        result = round_exact(F(200), fmt, RoundingMode.RNE)
        self.assertEqual(result.value, F(15))
        self.assertEqual(result.flags, frozenset({Flag.OVERFLOW, Flag.INEXACT}))


class UnderflowTests(unittest.TestCase):
    def test_tiny_inexact_raises_underflow_and_inexact(self) -> None:
        result = round_exact(F(1, 1000), SMALL, RoundingMode.RNE)
        self.assertEqual(result.value, FPValue.zero(1))
        self.assertEqual(result.flags, frozenset({Flag.UNDERFLOW, Flag.INEXACT}))

    def test_tiny_exact_subnormal_is_quiet(self) -> None:
        result = round_exact(F(1, 32), SMALL, RoundingMode.RNE)
        self.assertEqual(result.value, F(1, 32))
        self.assertEqual(result.flags, frozenset())

    def test_tiny_rounding_up_to_min_normal_is_not_underflow(self) -> None:
        # 255/1024 is below the min normal 1/4, but with destination precision
        # and an unbounded exponent range it rounds up to exactly 1/4.  IEEE
        # 754 detects tininess *after* rounding, so this is inexact only.
        self.assertLess(F(255, 1024), SMALL.min_normal)
        result = round_exact(F(255, 1024), SMALL, RoundingMode.RNE)
        self.assertEqual(result.value, F(1, 4))
        self.assertEqual(result.flags, frozenset({Flag.INEXACT}))

    def test_tiny_inexact_that_stays_tiny_underflows(self) -> None:
        # 15/64 is below 1/4, is exactly representable with unbounded exponent
        # (significand 15), so it stays tiny after unbounded rounding; the
        # bounded result rounds up to 1/4 and is inexact -> underflow.
        self.assertLess(F(15, 64), SMALL.min_normal)
        result = round_exact(F(15, 64), SMALL, RoundingMode.RNE)
        self.assertEqual(result.value, F(1, 4))
        self.assertEqual(result.flags, frozenset({Flag.UNDERFLOW, Flag.INEXACT}))

    def test_tiny_rounding_to_subnormal_underflows(self) -> None:
        result = round_exact(F(7, 64), SMALL, RoundingMode.RNE)
        self.assertEqual(result.value, F(1, 8))
        self.assertEqual(result.flags, frozenset({Flag.UNDERFLOW, Flag.INEXACT}))

    def test_smallest_subnormal_times_a_bit_underflows(self) -> None:
        # 3/64 lies between 1/32 and 1/16 and must round to a subnormal.
        result = round_exact(F(3, 64), SMALL, RoundingMode.RNE)
        self.assertEqual(result.value, F(1, 16))
        self.assertEqual(result.flags, frozenset({Flag.UNDERFLOW, Flag.INEXACT}))

    def test_underflow_requires_inexact(self) -> None:
        for value in (F(1, 32), F(3, 32), F(7, 32)):
            self.assertEqual(
                round_exact(value, SMALL, RoundingMode.RNE).flags, frozenset()
            )


class DirectedRoundingConsistencyTests(unittest.TestCase):
    """RU and RD must bracket RZ consistently across a sweep of values."""

    def test_directed_bracketing(self) -> None:
        values = [
            F(n, 64) for n in range(-900, 901, 7)
        ] + [F(1, 3), F(-2, 7), F(17, 5), F(155, 10)]
        for value in values:
            if abs(value) > SMALL.max_finite:
                continue
            rz = round_exact(value, SMALL, RoundingMode.RZ).value
            rd = round_exact(value, SMALL, RoundingMode.RD).value
            ru = round_exact(value, SMALL, RoundingMode.RU).value
            for result in (rz, rd, ru):
                self.assertTrue(result.is_finite, (value, result))
            rd_value, rz_value, ru_value = rd.exact, rz.exact, ru.exact
            self.assertLessEqual(rd_value, rz_value, value)
            self.assertLessEqual(rz_value, ru_value, value)
            self.assertLessEqual(rd_value, value)
            self.assertGreaterEqual(ru_value, value)

    def test_overflow_keeps_directed_bracketing_with_infinity(self) -> None:
        for value in (F(1000), F(-1000)):
            results = {
                mode: round_exact(value, SMALL, mode).value for mode in RoundingMode
            }
            for mode, result in results.items():
                self.assertIn(Flag.OVERFLOW, round_exact(value, SMALL, mode).flags, mode)
            # Toward zero always stays finite at the largest magnitude.
            self.assertEqual(results[RoundingMode.RZ], FPValue.finite(15, sign=1 if value > 0 else -1))


if __name__ == "__main__":
    unittest.main()
