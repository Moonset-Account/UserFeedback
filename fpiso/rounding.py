"""Stateless single rounding of exact rationals into a format.

This module is pure: it never reads or writes domain state.  ``round_exact``
returns the rounded value and the set of status flags implied by the rounding.
The caller (a protection domain) decides how those flags are latched and
masked, which keeps the arithmetic and the isolation policy independent.

Rules implemented
-----------------
* Overflow is raised iff the infinitely precise result, rounded to the
  destination precision but with an unbounded exponent range, would have a
  magnitude larger than the format's largest finite magnitude.  The delivered
  value is an infinity or the largest finite magnitude, depending on the
  rounding attribute and the sign, following IEEE 754.
* Underflow (tininess detected *after* rounding) is raised iff the rounded
  result is inexact and its magnitude is strictly smaller than the smallest
  positive normal number.  A tiny value that rounds up to the smallest normal
  is inexact but not underflowing.
* Inexact is raised iff the rounded result differs from the infinitely precise
  result.  Overflow always implies inexact.
* A zero result takes the sign of the infinitely precise result.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from .flags import Flag, flags_text
from .format import Format, RoundingMode, _floor_log2
from .value import FPValue


@dataclass(frozen=True)
class Rounded:
    """Result of one rounding plus the flags it implies."""

    value: FPValue
    flags: frozenset[Flag]

    @property
    def overflow(self) -> bool:
        return Flag.OVERFLOW in self.flags

    @property
    def underflow(self) -> bool:
        return Flag.UNDERFLOW in self.flags

    @property
    def inexact(self) -> bool:
        return Flag.INEXACT in self.flags

    def __str__(self) -> str:
        return f"{self.value} flags={flags_text(self.flags)}"


def _overflow_value(fmt: Format, rounding: RoundingMode, sign: int) -> FPValue:
    """Value delivered on overflow for ``sign`` under ``rounding``."""

    if rounding in (RoundingMode.RNE, RoundingMode.RNA):
        to_infinity = True
    elif rounding is RoundingMode.RU:
        to_infinity = sign > 0
    elif rounding is RoundingMode.RD:
        to_infinity = sign < 0
    else:  # RZ
        to_infinity = False
    if to_infinity and fmt.has_infinity:
        return FPValue.inf(sign)
    return FPValue.finite(fmt.max_finite, sign=sign)


def round_exact(
    exact: Fraction,
    fmt: Format,
    rounding: RoundingMode,
    zero_sign: int = 1,
) -> Rounded:
    """Round the exact rational ``exact`` into ``fmt`` under ``rounding``.

    ``zero_sign`` selects the sign of a zero result when ``exact`` is exactly
    zero (IEEE style: +0 unless directed downward).
    """

    if not isinstance(exact, Fraction):
        exact = Fraction(exact)
    if not isinstance(fmt, Format):
        raise TypeError("fmt must be a Format")
    if zero_sign not in (-1, 1):
        raise ValueError("zero_sign must be +1 or -1")

    if exact == 0:
        # The caller decides the sign of a zero result (IEEE 754 has separate
        # rules for sums, products and containments); the rounding attribute
        # does not override it here.
        return Rounded(FPValue.zero(zero_sign), frozenset())

    sign = 1 if exact > 0 else -1
    magnitude = abs(exact)
    exponent = _floor_log2(magnitude)
    unbounded_step = exponent - (fmt.precision - 1)

    # Round the *signed* value: directed modes must act in the correct
    # direction for negative numbers too.
    unbounded = fmt.quantize(exact, unbounded_step, rounding)

    if abs(unbounded) > fmt.max_finite:
        flags = frozenset({Flag.OVERFLOW, Flag.INEXACT})
        return Rounded(_overflow_value(fmt, rounding, sign), flags)

    step = min(fmt.step_max, max(fmt.step_min, unbounded_step))
    rounded = fmt.quantize(exact, step, rounding)

    flags: set[Flag] = set()
    if rounded != exact:
        flags.add(Flag.INEXACT)
        # Tininess detected after rounding (IEEE 754): the result computed as
        # though the exponent range were unbounded -- but still with destination
        # precision -- is strictly smaller in magnitude than the smallest
        # normal.  Underflow is signaled when the result is tiny *and* inexact.
        if abs(unbounded) < fmt.min_normal:
            flags.add(Flag.UNDERFLOW)

    if rounded == 0:
        return Rounded(FPValue.zero(sign), frozenset(flags))
    return Rounded(FPValue.finite(rounded), frozenset(flags))


def round_value(value: FPValue, fmt: Format, rounding: RoundingMode) -> Rounded:
    """Round an already-formed ``FPValue`` (used to build format values)."""

    if value.is_infinite:
        if not fmt.has_infinity:
            raise ValueError(f"{fmt.name} has no infinity")
        return Rounded(value, frozenset())
    return round_exact(value.exact, fmt, rounding, zero_sign=value.sign)


# Readable alias used by the operations layer.
round_rational = round_exact
