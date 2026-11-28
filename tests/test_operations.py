"""Tests for add, multiply and one-rounding multiply-add."""

from __future__ import annotations

import unittest
from decimal import Decimal
from fractions import Fraction as F

from fpiso.domain import Domain
from fpiso.errors import OperandError
from fpiso.flags import Flag
from fpiso.format import SMALL, RoundingMode
from fpiso.value import FPValue


def make(rounding=RoundingMode.RNE, fmt=SMALL) -> Domain:
    return Domain("op-test", fmt, rounding)


NEG_ZERO = FPValue.zero(-1)
POS_ZERO = FPValue.zero(1)


class OperandParsingTests(unittest.TestCase):
    def test_exact_types_accepted(self) -> None:
        domain = make()
        for value in (2, F(3, 8), Decimal("0.5"), "0.25", "-7/8"):
            domain.convert(value)

    def test_binary_float_rejected(self) -> None:
        domain = make()
        with self.assertRaises(OperandError):
            domain.add(0.1, 1)

    def test_bool_rejected(self) -> None:
        domain = make()
        with self.assertRaises(OperandError):
            domain.add(True, 1)

    def test_bad_string_rejected(self) -> None:
        domain = make()
        with self.assertRaises(OperandError):
            domain.add("banana", 1)

    def test_non_finite_decimal_rejected(self) -> None:
        domain = make()
        with self.assertRaises(OperandError):
            domain.add(Decimal("Infinity"), 1)


class AdditionTests(unittest.TestCase):
    def test_exact_sum_is_quiet(self) -> None:
        result = make().add(F(1, 4), F(1, 4))
        self.assertEqual(result.value, F(1, 2))
        self.assertEqual(result.flags, frozenset())

    def test_inexact_sum(self) -> None:
        result = make().add(F(1, 3), F(1, 3))
        self.assertEqual(result.value, F(11, 16))
        self.assertEqual(result.flags, frozenset({Flag.INEXACT}))

    def test_midpoint_obeys_domain_rounding(self) -> None:
        self.assertEqual(
            make(RoundingMode.RNE).add(1, F(1, 16)).value, F(1)
        )
        self.assertEqual(
            make(RoundingMode.RNA).add(1, F(1, 16)).value, F(9, 8)
        )
        self.assertEqual(
            make(RoundingMode.RU).add(1, F(1, 16)).value, F(9, 8)
        )
        self.assertEqual(
            make(RoundingMode.RD).add(-1, F(-1, 16)).value, F(-9, 8)
        )

    def test_overflow_in_add(self) -> None:
        result = make().add(15, 15)
        self.assertTrue(result.value.is_infinite)
        self.assertEqual(result.flags, frozenset({Flag.OVERFLOW, Flag.INEXACT}))

    def test_cancellation_to_zero_is_exact(self) -> None:
        result = make().add(F(9, 8), F(-9, 8))
        self.assertEqual(result.value, 0)
        self.assertEqual(result.flags, frozenset())
        self.assertEqual(str(result.value), "+0")

    def test_negative_zero_plus_negative_zero(self) -> None:
        domain = make()
        result = domain.add(NEG_ZERO, NEG_ZERO)
        self.assertEqual(result.value.sign, -1)
        self.assertEqual(str(result.value), "-0")
        self.assertEqual(result.flags, frozenset())

    def test_opposite_signed_zeros_under_rd_gives_negative_zero(self) -> None:
        rd_domain = Domain("rd", SMALL, RoundingMode.RD)
        result = rd_domain.add(POS_ZERO, NEG_ZERO)
        self.assertEqual(str(result.value), "-0")
        # Under RNE and RU the exact-zero sum of opposite zeros is +0.
        self.assertEqual(str(make(RoundingMode.RNE).add(POS_ZERO, NEG_ZERO).value), "+0")
        self.assertEqual(str(make(RoundingMode.RU).add(POS_ZERO, NEG_ZERO).value), "+0")


class MultiplicationTests(unittest.TestCase):
    def test_exact_product(self) -> None:
        result = make().mul(F(3, 2), F(2, 3))
        self.assertEqual(result.value, F(1))
        self.assertEqual(result.flags, frozenset())

    def test_inexact_product(self) -> None:
        result = make().mul(F(1, 3), F(1, 3))
        self.assertEqual(result.value, F(1, 8))
        self.assertEqual(result.flags, frozenset({Flag.UNDERFLOW, Flag.INEXACT}))

    def test_overflow_product(self) -> None:
        result = make().mul(8, 8)
        self.assertTrue(result.value.is_infinite)
        self.assertEqual(result.flags, frozenset({Flag.OVERFLOW, Flag.INEXACT}))

    def test_zero_sign_is_xor(self) -> None:
        positive = Domain("p", SMALL, RoundingMode.RNE).mul(0, 1)
        self.assertEqual(str(positive.value), "+0")
        self.assertEqual(str(make().mul(NEG_ZERO, 1).value), "-0")
        self.assertEqual(str(make().mul(NEG_ZERO, -1).value), "+0")
        self.assertEqual(str(make().mul(NEG_ZERO, NEG_ZERO).value), "+0")

    def test_small_product_underflows_to_zero(self) -> None:
        result = make().mul(F(1, 64), F(1, 64))
        self.assertEqual(result.value, 0)
        self.assertEqual(result.flags, frozenset({Flag.UNDERFLOW, Flag.INEXACT}))


class FmaTests(unittest.TestCase):
    """a*b+c must round only once, keeping the product exact."""

    def test_fused_single_rounding_differs_from_split(self) -> None:
        # 9/8 * 9/8 - 5/4 = 81/64 - 80/64 = 1/64, a tiny inexact nonzero.
        fused = make().fma(F(9, 8), F(9, 8), F(-5, 4))
        self.assertEqual(fused.product, F(81, 64))
        self.assertEqual(fused.exact, F(1, 64))
        self.assertEqual(fused.result.value, 0)
        self.assertEqual(
            fused.result.flags, frozenset({Flag.UNDERFLOW, Flag.INEXACT})
        )

        split_domain = make()
        product = split_domain.mul(F(9, 8), F(9, 8))
        self.assertEqual(product.value, F(5, 4))  # already rounded
        self.assertEqual(product.flags, frozenset({Flag.INEXACT}))
        total = split_domain.add(product.value, F(-5, 4))
        self.assertEqual(total.value, 0)
        self.assertEqual(total.flags, frozenset())

    def test_exact_fma_is_quiet(self) -> None:
        trace = make().fma(2, 3, 4)
        self.assertEqual(trace.result.value, F(10))
        self.assertEqual(trace.result.flags, frozenset())

    def test_fma_overflow_via_exact_product(self) -> None:
        # 8 * 2 = 16 overflows even though 8+... stays representable.
        trace = make().fma(8, 2, 0)
        self.assertTrue(trace.result.value.is_infinite)
        self.assertIn(Flag.OVERFLOW, trace.result.flags)

    def test_fma_zero_sign(self) -> None:
        # (+0 * -3) + -0: product is -0, addend -0, same sign -> -0.
        trace = make().fma(0, -3, NEG_ZERO)
        self.assertEqual(str(trace.result.value), "-0")
        # (-0 * 3) + -0: product is -0, addend -0 -> -0.
        self.assertEqual(str(make().fma(NEG_ZERO, 3, NEG_ZERO).result.value), "-0")
        # (-0 * -3) is +0, so (+0) + (-0) has opposite signs -> +0.
        self.assertEqual(str(make().fma(NEG_ZERO, -3, NEG_ZERO).result.value), "+0")
        # Opposite-signed zeros under RD become -0.
        self.assertEqual(
            str(make(RoundingMode.RD).fma(NEG_ZERO, -3, NEG_ZERO).result.value), "-0"
        )

    def test_fma_exact_cancellation_sign(self) -> None:
        # 2*3 - 6 is exact cancellation of a nonzero product: +0, or -0 under RD.
        for mode, expected in (
            (RoundingMode.RNE, "+0"),
            (RoundingMode.RU, "+0"),
            (RoundingMode.RD, "-0"),
        ):
            trace = make(mode).fma(2, 3, -6)
            self.assertEqual(str(trace.result.value), expected, mode)
            self.assertEqual(trace.result.flags, frozenset())

    def test_fma_counterexample_exposed_by_exact_product(self) -> None:
        # a*b overflows the destination but a*b-c is exactly representable:
        # a fused implementation returns 1/8 and reports no overflow.
        trace = make().fma(8, 2, F(-255, 16))
        self.assertEqual(trace.product, F(16))
        self.assertEqual(trace.exact, F(1, 16))
        self.assertEqual(trace.result.value, F(1, 16))
        self.assertEqual(trace.result.flags, frozenset())

        split = make()
        product = split.mul(8, 2)
        self.assertTrue(product.value.is_infinite)
        self.assertIn(Flag.OVERFLOW, product.flags)

    def test_fma_trace_records_intermediates(self) -> None:
        trace = make(RoundingMode.RU).fma(F(1, 3), F(1, 3), F(-1, 8))
        self.assertEqual(trace.a, F(1, 3))
        self.assertEqual(trace.product, F(1, 9))
        self.assertEqual(trace.exact, F(1, 9) - F(1, 8))
        self.assertAlmostEqual(float(trace.exact), 1 / 9 - 1 / 8)

    def test_fma_over_fused_operand_types(self) -> None:
        trace = make().fma("0.5", Decimal("0.25"), F(-1, 8))
        self.assertEqual(trace.exact, F(0))
        self.assertEqual(trace.result.value, 0)
        self.assertEqual(trace.result.flags, frozenset())
        # And a case that lands exactly on a representable value.
        trace = make().fma("0.5", Decimal("0.5"), F(1, 8))
        self.assertEqual(trace.result.value, F(3, 8))
        self.assertEqual(trace.result.flags, frozenset())


class ConvertTests(unittest.TestCase):
    def test_convert_rounds_once_and_flags(self) -> None:
        result = make().convert(F(1, 3))
        self.assertEqual(result.value, F(11, 32))
        self.assertEqual(result.flags, frozenset({Flag.INEXACT}))

    def test_convert_keeps_signed_zero(self) -> None:
        result = make().convert(NEG_ZERO)
        self.assertEqual(result.value.sign, -1)
        self.assertEqual(result.flags, frozenset())


if __name__ == "__main__":
    unittest.main()
