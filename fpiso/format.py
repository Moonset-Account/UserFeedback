"""Small configurable binary floating-point formats.

Value model
-----------
A format has a precision ``p`` (significand bits, including the implicit
leading bit for normal numbers), a minimum exponent ``emin`` and a maximum
exponent ``emax``.  Representable finite magnitudes are exactly the set

    { k * 2**s  :  step_min <= s <= step_max,
                   2**(p-1) <= k < 2**p }            (normals)
  ∪ { k * 2**step_min : 1 <= k < 2**(p-1) }            (subnormals)
  ∪ { 0 }                                              (signed zero)

with ``step_min = emin - (p - 1)`` and ``step_max = emax - (p - 1)``.  The
largest finite magnitude is ``(2**p - 1) * 2**step_max``.

Rounding an exact rational therefore means picking a point on this grid.  All
grid arithmetic below is exact ``Fraction`` arithmetic; nothing is delegated to
the host FPU.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from fractions import Fraction

from .errors import FormatError


class RoundingMode(str, Enum):
    """Supported rounding attributes."""

    RNE = "rne"  # nearest, ties to even
    RNA = "rna"  # nearest, ties away from zero
    RZ = "rz"    # toward zero
    RD = "rd"    # toward -infinity
    RU = "ru"    # toward +infinity


ROUNDING_ORDER: tuple[RoundingMode, ...] = (
    RoundingMode.RNE,
    RoundingMode.RNA,
    RoundingMode.RZ,
    RoundingMode.RD,
    RoundingMode.RU,
)


def coerce_rounding(mode: "RoundingMode | str") -> RoundingMode:
    if isinstance(mode, RoundingMode):
        return mode
    try:
        return RoundingMode(str(mode).lower())
    except ValueError as exc:
        raise FormatError(
            f"unknown rounding mode {mode!r}; expected one of "
            f"{', '.join(m.value for m in ROUNDING_ORDER)}"
        ) from exc


def _floor_log2(value: Fraction) -> int:
    """Return ``e`` with ``2**e <= value < 2**(e+1)`` for ``value > 0``."""

    if value <= 0:
        raise ValueError("value must be positive")
    num, den = value.numerator, value.denominator
    e = num.bit_length() - den.bit_length()
    if value < Fraction(2) ** e:
        e -= 1
    return e


@dataclass(frozen=True)
class Format:
    """An immutable small binary floating-point format."""

    name: str
    precision: int
    emin: int
    emax: int
    has_infinity: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise FormatError("format name must be a non-empty string")
        for field_name in ("precision", "emin", "emax"):
            value = getattr(self, field_name)
            if not isinstance(value, int) or isinstance(value, bool):
                raise FormatError(f"{field_name} must be an int")
        if not isinstance(self.has_infinity, bool):
            raise FormatError("has_infinity must be bool")
        if self.precision < 1:
            raise FormatError("precision must be >= 1")
        if self.emax < self.emin:
            raise FormatError("emax must be >= emin")

    # -- derived quantities -------------------------------------------------
    @property
    def step_min(self) -> int:
        """Exponent of the subnormal grid (the smallest ULP)."""

        return self.emin - (self.precision - 1)

    @property
    def step_max(self) -> int:
        """Exponent of the normal grid at the top of the range."""

        return self.emax - (self.precision - 1)

    @property
    def min_normal(self) -> Fraction:
        return Fraction(2) ** self.emin

    @property
    def min_subnormal(self) -> Fraction:
        return Fraction(2) ** self.step_min

    @property
    def max_finite(self) -> Fraction:
        return (2**self.precision - 1) * Fraction(2) ** self.step_max

    @property
    def max_significand(self) -> int:
        return (1 << self.precision) - 1

    def describe(self) -> str:
        infinity = "inf" if self.has_infinity else "finite-only"
        return (
            f"{self.name}(p={self.precision}, emin={self.emin}, emax={self.emax}, "
            f"step=[{self.step_min}, {self.step_max}], max={self.max_finite}, "
            f"{infinity})"
        )

    # -- exact grid helpers -------------------------------------------------
    @staticmethod
    def snap(value: Fraction, rounding: RoundingMode) -> int:
        """Round an exact ``Fraction`` to an integer under ``rounding``."""

        floor = value.numerator // value.denominator
        remainder = value - floor
        if remainder == 0:
            return floor
        if rounding is RoundingMode.RD:
            return floor
        if rounding is RoundingMode.RU:
            return floor + 1
        if rounding is RoundingMode.RZ:
            return floor if value > 0 else floor + 1
        half = Fraction(1, 2)
        if remainder < half:
            return floor
        if remainder > half:
            return floor + 1
        if rounding is RoundingMode.RNA:
            return floor if value < 0 else floor + 1
        return floor if floor % 2 == 0 else floor + 1  # RNE

    def quantize(
        self, value: Fraction, step_exp: int, rounding: RoundingMode
    ) -> Fraction:
        """Round the signed ``value`` to a multiple of ``2**step_exp``."""

        if not isinstance(value, Fraction):
            value = Fraction(value)
        unit = Fraction(2) ** step_exp
        return self.snap(value / unit, rounding) * unit

    def _significand_range(self, step_exp: int) -> tuple[int, int]:
        """Allowed significand range on the grid at ``step_exp``."""

        lowest = 1 if step_exp == self.step_min else 1 << (self.precision - 1)
        return lowest, (1 << self.precision) - 1

    def significand_range(self, step_exp: int) -> tuple[int, int]:
        """Public form of :meth:`_significand_range` for inspection/tests."""

        if step_exp < self.step_min or step_exp > self.step_max:
            raise FormatError(
                f"step exponent {step_exp} outside "
                f"[{self.step_min}, {self.step_max}]"
            )
        return self._significand_range(step_exp)

    def representable_at(self, magnitude: Fraction, step_exp: int) -> bool:
        """True if non-negative ``magnitude`` lies on the grid at ``step_exp``."""

        if magnitude < 0:
            raise ValueError("magnitude must be non-negative")
        if step_exp < self.step_min or step_exp > self.step_max:
            return False
        if magnitude == 0:
            return step_exp == self.step_min
        scaled = magnitude / (Fraction(2) ** step_exp)
        if scaled.denominator != 1:
            return False
        lowest, highest = self._significand_range(step_exp)
        return lowest <= scaled.numerator <= highest

    def is_representable(self, value: Fraction) -> bool:
        if value == 0:
            return True
        magnitude = abs(value)
        if magnitude > self.max_finite:
            return False
        step = max(
            self.step_min,
            min(self.step_max, _floor_log2(magnitude) - (self.precision - 1)),
        )
        return self.representable_at(magnitude, step)

    def neighbors(self, magnitude: Fraction) -> tuple[Fraction, Fraction]:
        """Bracket a positive ``magnitude`` with representable magnitudes.

        Returns ``(below, above)`` where ``below`` is the largest representable
        magnitude ``<= magnitude`` (0 if ``magnitude`` is below the smallest
        subnormal) and ``above`` is the smallest representable magnitude
        ``>= magnitude`` (``max_finite`` if ``magnitude`` is beyond the top of
        the range, so callers must check for overflow separately).
        """

        if magnitude <= 0:
            raise ValueError("magnitude must be positive")
        below = Fraction(0)
        above: Fraction | None = None
        for step in range(self.step_min, self.step_max + 1):
            unit = Fraction(2) ** step
            lowest, highest = self._significand_range(step)
            scaled = magnitude / unit
            k_floor = scaled.numerator // scaled.denominator
            k_ceil = k_floor if scaled.denominator == 1 else k_floor + 1

            if k_floor > highest:
                candidate = highest * unit
            elif k_floor >= lowest:
                candidate = k_floor * unit
            else:
                candidate = None
            if candidate is not None and candidate <= magnitude and candidate > below:
                below = candidate

            if k_ceil < lowest:
                candidate_up = lowest * unit
            elif k_ceil <= highest:
                candidate_up = k_ceil * unit
            else:
                candidate_up = None
            if candidate_up is not None and candidate_up >= magnitude:
                if above is None or candidate_up < above:
                    above = candidate_up

        if above is None:
            above = self.max_finite
        return below, above

    def predecessor(self, value: Fraction) -> Fraction:
        """Largest representable value strictly below ``value``."""

        if not self.is_representable(value):
            raise FormatError(f"{value} is not representable in {self.name}")
        if value <= 0:
            return -self.successor(-value)
        best = Fraction(0)
        for step in range(self.step_min, self.step_max + 1):
            unit = Fraction(2) ** step
            scaled = value / unit
            if scaled.denominator != 1:
                continue
            candidate = (scaled.numerator - 1) * unit
            if candidate <= 0 or candidate >= value:
                continue
            if self.representable_at(candidate, step) and candidate > best:
                best = candidate
        return best

    def successor(self, value: Fraction) -> Fraction:
        """Smallest representable value strictly above ``value``."""

        if not self.is_representable(value):
            raise FormatError(f"{value} is not representable in {self.name}")
        if value < 0:
            return -self.predecessor(-value)
        if value == 0:
            return self.min_subnormal
        best: Fraction | None = None
        for step in range(self.step_min, self.step_max + 1):
            unit = Fraction(2) ** step
            scaled = value / unit
            if scaled.denominator != 1:
                continue
            candidate = (scaled.numerator + 1) * unit
            if candidate <= value:
                continue
            if self.representable_at(candidate, step):
                if best is None or candidate < best:
                    best = candidate
        if best is None:
            raise FormatError(f"no finite successor for {value} in {self.name}")
        return best


#: Demonstrator format: 4 significand bits, exponent range 2**-2 .. 2**3,
#: subnormal step 2**-5, largest finite value 15.
SMALL = Format(name="small", precision=4, emin=-2, emax=3)
