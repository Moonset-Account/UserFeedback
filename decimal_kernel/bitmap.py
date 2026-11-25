"""Null bitmap handling.

The bitmap is deliberately interpreted independently from BCD payload bytes.
The conversion loop checks :meth:`NullBitmap.is_null` before touching a row
payload.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Sequence

from .errors import BitmapError, ConfigurationError


class BitOrder(str, Enum):
    """Bit numbering inside raw bitmap bytes."""

    MSB = "msb"
    LSB = "lsb"


def normalize_bit_order(value: BitOrder | str) -> BitOrder:
    if isinstance(value, BitOrder):
        return value
    if not isinstance(value, str):
        raise ConfigurationError("bit order must be 'msb' or 'lsb'")
    normalized = value.strip().lower().replace("-", "").replace("_", "")
    if normalized in ("msb", "highbitfirst", "msbfirst"):
        return BitOrder.MSB
    if normalized in ("lsb", "lowbitfirst", "lsbfirst"):
        return BitOrder.LSB
    raise ConfigurationError("bit order must be 'msb' or 'lsb'")


@dataclass(frozen=True)
class NullBitmap:
    """An immutable row null map.

    ``flags[i]`` is ``True`` exactly when row ``i`` is SQL NULL.
    """

    flags: tuple[bool, ...]
    bit_order: BitOrder = BitOrder.MSB

    @classmethod
    def all_non_null(cls, row_count: int) -> "NullBitmap":
        _validate_count(row_count)
        return cls(tuple(False for _ in range(row_count)), BitOrder.MSB)

    @classmethod
    def all_null(cls, row_count: int) -> "NullBitmap":
        _validate_count(row_count)
        return cls(tuple(True for _ in range(row_count)), BitOrder.MSB)

    @classmethod
    def from_bools(
        cls,
        flags: Sequence[bool | int],
        bit_order: BitOrder | str = BitOrder.MSB,
    ) -> "NullBitmap":
        order = normalize_bit_order(bit_order)
        values: list[bool] = []
        for index, flag in enumerate(flags):
            if isinstance(flag, bool):
                values.append(flag)
            elif isinstance(flag, int) and flag in (0, 1):
                values.append(bool(flag))
            else:
                raise ConfigurationError(
                    f"bitmap flag at index {index} must be bool or 0/1"
                )
        return cls(tuple(values), order)

    @classmethod
    def from_bytes(
        cls,
        data: bytes | bytearray | memoryview,
        row_count: int,
        bit_order: BitOrder | str = BitOrder.MSB,
    ) -> "NullBitmap":
        _validate_count(row_count)
        order = normalize_bit_order(bit_order)
        required = (row_count + 7) // 8
        try:
            data_len = len(data)
        except TypeError as exc:
            raise ConfigurationError("bitmap data must be bytes-like") from exc
        if data_len < required:
            raise BitmapError(
                f"bitmap needs {required} byte(s) for {row_count} rows, "
                f"got {data_len}"
            )

        flags: list[bool] = []
        for row in range(row_count):
            byte = data[row // 8]
            bit_in_byte = row % 8
            if order is BitOrder.MSB:
                bit = (byte >> (7 - bit_in_byte)) & 1
            else:
                bit = (byte >> bit_in_byte) & 1
            flags.append(bool(bit))
        return cls(tuple(flags), order)

    def __len__(self) -> int:
        return len(self.flags)

    def is_null(self, row: int) -> bool:
        return self.flags[row]

    def with_error_rows_null(
        self, error_rows: Sequence[int]
    ) -> "NullBitmap":
        flags = list(self.flags)
        for row in error_rows:
            flags[row] = True
        return NullBitmap(tuple(flags), self.bit_order)

    def to_bytes(self, bit_order: BitOrder | str | None = None) -> bytes:
        order = self.bit_order if bit_order is None else normalize_bit_order(bit_order)
        output = bytearray((len(self.flags) + 7) // 8)
        for row, flag in enumerate(self.flags):
            if not flag:
                continue
            byte_index = row // 8
            bit_in_byte = row % 8
            if order is BitOrder.MSB:
                output[byte_index] |= 0x80 >> bit_in_byte
            else:
                output[byte_index] |= 1 << bit_in_byte
        return bytes(output)


def _validate_count(row_count: int) -> None:
    if isinstance(row_count, bool) or not isinstance(row_count, int):
        raise ConfigurationError("row_count must be an integer")
    if row_count < 0:
        raise ConfigurationError("row_count must not be negative")


def coerce_null_bitmap(
    value: "NullBitmap | bytes | bytearray | memoryview | Sequence[bool | int] | None",
    row_count: int,
    bit_order: BitOrder | str = BitOrder.MSB,
) -> NullBitmap:
    """Normalize the supported bitmap inputs and check the row count."""

    _validate_count(row_count)
    order = normalize_bit_order(bit_order)

    if value is None:
        return NullBitmap.all_non_null(row_count)

    if isinstance(value, NullBitmap):
        if len(value) != row_count:
            raise BitmapError(
                f"bitmap has {len(value)} rows but payload batch has {row_count}"
            )
        return value

    if isinstance(value, (bytes, bytearray, memoryview)):
        return NullBitmap.from_bytes(value, row_count, order)

    if isinstance(value, str):
        raise ConfigurationError("bitmap cannot be a string")

    if isinstance(value, Sequence):
        if len(value) != row_count:
            raise BitmapError(
                f"bitmap has {len(value)} flags but payload batch has {row_count}"
            )
        return NullBitmap.from_bools(value, order)

    raise ConfigurationError(
        "bitmap must be NullBitmap, bytes-like, a bool/0-1 sequence, or None"
    )
