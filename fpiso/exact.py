"""Conversion of user operands to arbitrary-precision rationals.

Only exact inputs are accepted.  Binary ``float`` is deliberately rejected:
admitting a host binary float would import a host-FPU rounding step into the
model before the domain-specific rounding has any chance to run.
"""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction
from typing import TypeAlias

from .errors import OperandError

#: The model's operand/result type: an exact arbitrary-precision rational.
Exact: TypeAlias = Fraction


def as_exact(value: object) -> Fraction:
    """Convert an exact numeric input to ``Fraction``.

    Accepted: ``int`` (but not ``bool``), ``Fraction``, ``Decimal``, and
    exact strings accepted by ``Fraction`` (for example ``"0.125"`` or
    ``"3/8"``).  ``float`` and everything else raises ``OperandError``.
    """

    if isinstance(value, Fraction):
        return value
    if isinstance(value, bool):
        raise OperandError("bool is not a rational operand")
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise OperandError("non-finite Decimal is not an exact operand")
        return Fraction(value)
    if isinstance(value, str):
        try:
            return Fraction(value)
        except (ValueError, ZeroDivisionError) as exc:
            raise OperandError(f"invalid exact rational string: {value!r}") from exc
    raise OperandError(
        "operands must be exact rationals (int, Fraction, Decimal or an exact "
        f"string), got {type(value).__name__}; binary floats are not accepted"
    )
