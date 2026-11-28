"""Exact arithmetic kernels for the three required operations.

Every kernel takes exact operands, computes the infinitely precise result with
``Fraction``, and performs **exactly one** rounding into the destination
format.  No kernel touches domain state; they return a :class:`Rounded` which
carries the value and the implied status flags.  The runtime then decides how
to latch those flags for the active protection domain.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from .exact import as_exact
from .flags import Flag
from .format import Format, RoundingMode
from .rounding import Rounded, round_exact
from .value import FPValue


def _operand(value: object) -> FPValue:
    if isinstance(value, FPValue):
        if value.is_infinite:
            raise ValueError("operands must be finite; infinities are results only")
        return value
    return FPValue.finite(as_exact(value))


def _sign(value: FPValue) -> int:
    return value.sign


@dataclass(frozen=True)
class AddResult:
    rounded: Rounded
    exact: Fraction
    zero_sign: int

    @property
    def value(self) -> FPValue:
        return self.rounded.value

    @property
    def flags(self) -> frozenset[Flag]:
        return self.rounded.flags


@dataclass(frozen=True)
class MulResult:
    rounded: Rounded
    exact: Fraction
    zero_sign: int

    @property
    def value(self) -> FPValue:
        return self.rounded.value

    @property
    def flags(self) -> frozenset[Flag]:
        return self.rounded.flags


@dataclass(frozen=True)
class FmaResult:
    rounded: Rounded
    a: Fraction
    b: Fraction
    c: Fraction
    product: Fraction
    exact: Fraction

    @property
    def value(self) -> FPValue:
        return self.rounded.value

    @property
    def flags(self) -> frozenset[Flag]:
        return self.rounded.flags


def _zero_sum_sign(
    left: FPValue, right: FPValue, rounding: RoundingMode
) -> int:
    """Sign of a zero result from addition, per IEEE 754.

    Two zeros of the same sign keep that sign; every other exact-zero sum is
    ``+0`` except under round-toward-negative, where it is ``-0``.
    """

    if left.is_zero and right.is_zero and left.sign == right.sign:
        return left.sign
    return -1 if rounding is RoundingMode.RD else 1


def add(x: object, y: object, fmt: Format, rounding: RoundingMode) -> AddResult:
    """``x + y`` with a single rounding."""

    left = _operand(x)
    right = _operand(y)
    exact = left.exact + right.exact
    zero_sign = 1
    if exact == 0:
        zero_sign = _zero_sum_sign(left, right, rounding)
    rounded = round_exact(exact, fmt, rounding, zero_sign=zero_sign)
    return AddResult(rounded=rounded, exact=exact, zero_sign=zero_sign)


def mul(x: object, y: object, fmt: Format, rounding: RoundingMode) -> MulResult:
    """``x * y`` with a single rounding.  Zero results take the XOR sign."""

    left = _operand(x)
    right = _operand(y)
    exact = left.exact * right.exact
    zero_sign = _sign(left) * _sign(right)
    rounded = round_exact(exact, fmt, rounding, zero_sign=zero_sign)
    return MulResult(rounded=rounded, exact=exact, zero_sign=zero_sign)


def _zero_fma_sign(
    fa: FPValue,
    fb: FPValue,
    fc: FPValue,
    product: Fraction,
    rounding: RoundingMode,
) -> int:
    """Sign of a zero result from ``a*b+c`` per IEEE 754.

    * zero product and zero addend of the same sign: that sign;
    * zero product and zero addend of opposite signs: ``+0``, or ``-0`` under
      round-toward-negative;
    * exact cancellation of a nonzero product and the addend: ``+0``, or ``-0``
      under round-toward-negative.
    """

    if product == 0:
        product_sign = fa.sign * fb.sign
        if fc.is_zero and product_sign == fc.sign:
            return product_sign
    return -1 if rounding is RoundingMode.RD else 1


def fma(
    a: object,
    b: object,
    c: object,
    fmt: Format,
    rounding: RoundingMode,
) -> FmaResult:
    """``a * b + c`` with the product kept exact (one rounding overall)."""

    fa = _operand(a)
    fb = _operand(b)
    fc = _operand(c)
    product = fa.exact * fb.exact
    exact = product + fc.exact
    zero_sign = 1
    if exact == 0:
        zero_sign = _zero_fma_sign(fa, fb, fc, product, rounding)
    rounded = round_exact(exact, fmt, rounding, zero_sign=zero_sign)
    return FmaResult(
        rounded=rounded,
        a=fa.exact,
        b=fb.exact,
        c=fc.exact,
        product=product,
        exact=exact,
    )


def convert(value: object, fmt: Format, rounding: RoundingMode) -> Rounded:
    """Round one exact operand into ``fmt`` (a single rounding)."""

    operand = _operand(value)
    return round_exact(operand.exact, fmt, rounding, zero_sign=operand.sign)
