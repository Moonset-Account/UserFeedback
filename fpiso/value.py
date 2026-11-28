"""Result values for the floating-point isolation model.

A result is either a finite value (an exact ``Fraction``, including signed
zero) or an infinity.  NaN does not arise because invalid operations are
rejected by the model instead of being produced.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from .errors import FormatError


@dataclass(frozen=True, eq=False)
class FPValue:
    """A finite or infinite floating-point value."""

    sign: int
    kind: str  # "finite" or "inf"
    magnitude: Fraction = Fraction(0)

    def __post_init__(self) -> None:
        if self.sign not in (-1, 1):
            raise FormatError("sign must be +1 or -1")
        if self.kind not in ("finite", "inf"):
            raise FormatError("kind must be 'finite' or 'inf'")
        if not isinstance(self.magnitude, Fraction):
            raise FormatError("magnitude must be a Fraction")
        if self.magnitude < 0:
            raise FormatError("magnitude must be non-negative")
        if self.kind == "inf" and self.magnitude != 0:
            raise FormatError("infinite values have zero magnitude")

    # -- constructors -------------------------------------------------------
    @classmethod
    def finite(cls, value: Fraction | int, sign: int | None = None) -> "FPValue":
        """Build a finite value from a signed magnitude.

        With no ``sign`` the sign is taken from ``value``.  With an explicit
        ``sign``, ``value`` must already be a non-negative magnitude; passing a
        negative magnitude alongside a sign is rejected rather than silently
        re-signed.
        """

        value = Fraction(value)
        if sign is None:
            if value == 0:
                return cls.zero(1)
            return cls(
                sign=-1 if value < 0 else 1, kind="finite", magnitude=abs(value)
            )
        if value < 0:
            raise FormatError(
                "explicit sign requires a non-negative magnitude"
            )
        if value == 0:
            return cls.zero(sign)
        return cls(sign=sign, kind="finite", magnitude=value)

    @classmethod
    def zero(cls, sign: int = 1) -> "FPValue":
        return cls(sign=sign, kind="finite", magnitude=Fraction(0))

    @classmethod
    def inf(cls, sign: int = 1) -> "FPValue":
        return cls(sign=sign, kind="inf", magnitude=Fraction(0))

    # -- predicates ---------------------------------------------------------
    @property
    def is_finite(self) -> bool:
        return self.kind == "finite"

    @property
    def is_infinite(self) -> bool:
        return self.kind == "inf"

    @property
    def is_zero(self) -> bool:
        return self.kind == "finite" and self.magnitude == 0

    @property
    def exact(self) -> Fraction:
        """Exact signed value; raises for infinities."""

        if self.kind != "finite":
            raise FormatError("infinite value has no exact rational")
        return self.sign * self.magnitude

    def negated(self) -> "FPValue":
        return FPValue(sign=-self.sign, kind=self.kind, magnitude=self.magnitude)

    def __str__(self) -> str:
        if self.kind == "inf":
            return "+inf" if self.sign > 0 else "-inf"
        if self.magnitude == 0:
            return "+0" if self.sign > 0 else "-0"
        return str(self.exact)

    def __repr__(self) -> str:
        return f"FPValue({self})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, FPValue):
            return (
                self.sign == other.sign
                and self.kind == other.kind
                and self.magnitude == other.magnitude
            )
        if isinstance(other, (int, Fraction)):
            return self.kind == "finite" and self.exact == Fraction(other)
        return NotImplemented

    def __hash__(self) -> int:
        return hash((self.sign, self.kind, self.magnitude))
