"""Column specifications for packed BCD source and fixed-width target."""

from __future__ import annotations

from dataclasses import dataclass

from .bitmap import BitOrder, normalize_bit_order
from .errors import ConfigurationError


def _positive_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigurationError(f"{name} must be an integer")
    if value <= 0:
        raise ConfigurationError(f"{name} must be > 0")
    return value


def _non_negative_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigurationError(f"{name} must be an integer")
    if value < 0:
        raise ConfigurationError(f"{name} must be >= 0")
    return value


@dataclass(frozen=True)
class SourceColumn:
    """Packed signed DECIMAL column metadata.

    ``precision`` is the total number of BCD digits. ``scale`` is the number of
    digits after the decimal point and may exceed precision in some database
    dialects; integer-only conversion still remains well-defined.
    """

    precision: int
    scale: int = 0

    def __post_init__(self) -> None:
        _positive_int("precision", self.precision)
        _non_negative_int("source_scale", self.scale)

    @property
    def packed_width(self) -> int:
        """Number of bytes in IBM-style packed decimal storage."""

        return self.precision // 2 + 1


@dataclass(frozen=True)
class TargetColumn:
    """Fixed-width signed two's-complement integer target metadata."""

    scale: int = 0
    width: int = 8
    precision: int | None = None
    byte_order: str = "big"

    def __post_init__(self) -> None:
        _non_negative_int("target_scale", self.scale)
        _positive_int("target_width", self.width)
        if self.precision is not None:
            _positive_int("target_precision", self.precision)
        if self.byte_order not in ("big", "little"):
            raise ConfigurationError("byte_order must be 'big' or 'little'")

    @property
    def signed_min(self) -> int:
        return -(1 << (8 * self.width - 1))

    @property
    def signed_max(self) -> int:
        return (1 << (8 * self.width - 1)) - 1

    @property
    def decimal_max_integer(self) -> int | None:
        """Largest scaled integer allowed by an optional DECIMAL precision."""

        if self.precision is None:
            return None
        return 10**self.precision - 1

    def encode(self, value: int) -> bytes:
        return value.to_bytes(
            self.width,
            byteorder=self.byte_order,
            signed=True,
        )


@dataclass(frozen=True)
class KernelOptions:
    """Non-column conversion options."""

    bitmap_bit_order: BitOrder = BitOrder.MSB
    accept_unsigned_sign: bool = False

    def __post_init__(self) -> None:
        order = normalize_bit_order(self.bitmap_bit_order)
        if order is not self.bitmap_bit_order:
            object.__setattr__(self, "bitmap_bit_order", order)
        if not isinstance(self.accept_unsigned_sign, bool):
            raise ConfigurationError("accept_unsigned_sign must be bool")
