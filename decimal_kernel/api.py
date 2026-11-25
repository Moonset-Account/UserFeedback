"""Batch conversion API and contiguous database-page adapter."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Sequence

from .bitmap import (
    BitOrder,
    NullBitmap,
    coerce_null_bitmap,
    normalize_bit_order,
)
from .codec import convert_one_value
from .errors import (
    BatchConversionError,
    ConfigurationError,
    ErrorCode,
    RowError,
    _RowConversionFailure,
)
from .spec import SourceColumn, TargetColumn


class ErrorMode(str, Enum):
    """Batch error policy."""

    STRICT = "strict"
    LENIENT = "lenient"


def normalize_error_mode(value: ErrorMode | str) -> ErrorMode:
    if isinstance(value, ErrorMode):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("strict", "all_or_nothing", "reject_batch"):
            return ErrorMode.STRICT
        if normalized in ("lenient", "permissive", "row_null", "null_errors"):
            return ErrorMode.LENIENT
    raise ConfigurationError("error mode must be 'strict' or 'lenient'")


@dataclass(frozen=True)
class BatchResult:
    """Successful strict result or row-rejected lenient result."""

    values: tuple[int | None, ...]
    slots: tuple[bytes | None, ...]
    errors: tuple[RowError, ...]
    null_bitmap: NullBitmap
    target_width: int
    byte_order: str

    def __len__(self) -> int:
        return len(self.values)

    @property
    def row_count(self) -> int:
        return len(self.values)

    def contiguous_target_bytes(self, null_fill: bytes | None = None) -> bytes:
        """Concatenate fixed-width target slots.

        NULL/error slots are replaced by ``null_fill`` (zero bytes by default).
        Their SQL NULL state is carried by :attr:`null_bitmap`; callers should
        not interpret the filler as a value.
        """

        if null_fill is None:
            fill = bytes(self.target_width)
        else:
            if len(null_fill) != self.target_width:
                raise ConfigurationError(
                    f"null_fill must be {self.target_width} byte(s)"
                )
            fill = null_fill
        return b"".join(fill if slot is None else slot for slot in self.slots)


class PackedDecimalBatchConverter:
    """Stateful kernel bound to one source and one target column definition."""

    def __init__(
        self,
        *,
        precision: int,
        source_scale: int,
        target_scale: int,
        target_width: int = 8,
        target_precision: int | None = None,
        byte_order: str = "big",
        bitmap_bit_order: BitOrder | str = BitOrder.MSB,
        accept_unsigned_sign: bool = False,
    ) -> None:
        self.source = SourceColumn(precision=precision, scale=source_scale)
        self.target = TargetColumn(
            scale=target_scale,
            width=target_width,
            precision=target_precision,
            byte_order=byte_order,
        )
        self.bitmap_bit_order = normalize_bit_order(bitmap_bit_order)
        self.accept_unsigned_sign = accept_unsigned_sign

    def convert(
        self,
        payloads: Sequence[object],
        *,
        null_bitmap: NullBitmap
        | bytes
        | bytearray
        | memoryview
        | Sequence[bool | int]
        | None = None,
        mode: ErrorMode | str = ErrorMode.STRICT,
        bitmap_bit_order: BitOrder | str | None = None,
    ) -> BatchResult:
        return convert_packed_batch(
            payloads,
            precision=self.source.precision,
            source_scale=self.source.scale,
            target_scale=self.target.scale,
            target_width=self.target.width,
            target_precision=self.target.precision,
            byte_order=self.target.byte_order,
            null_bitmap=null_bitmap,
            bitmap_bit_order=(
                self.bitmap_bit_order
                if bitmap_bit_order is None
                else bitmap_bit_order
            ),
            on_error=mode,
            accept_unsigned_sign=self.accept_unsigned_sign,
        )


def convert_packed_batch(
    payloads: Sequence[object],
    *,
    precision: int,
    source_scale: int,
    target_scale: int,
    target_width: int = 8,
    target_precision: int | None = None,
    byte_order: str = "big",
    null_bitmap: NullBitmap
    | bytes
    | bytearray
    | memoryview
    | Sequence[bool | int]
    | None = None,
    bitmap_bit_order: BitOrder | str = BitOrder.MSB,
    on_error: ErrorMode | str = ErrorMode.STRICT,
    accept_unsigned_sign: bool = False,
) -> BatchResult:
    """Convert a batch of packed DECIMAL payloads to fixed-width signed bytes.

    Strict mode collects all row errors and then raises
    :class:`BatchConversionError`; no partial :class:`BatchResult` is returned.
    Lenient mode sets each erroneous row to SQL NULL and returns its
    :class:`RowError`.

    The payload slot is indexed only after checking the bitmap. Consequently,
    arbitrary/undefined bytes in NULL slots are never decoded.
    """

    source = SourceColumn(precision=precision, scale=source_scale)
    target = TargetColumn(
        scale=target_scale,
        width=target_width,
        precision=target_precision,
        byte_order=byte_order,
    )
    mode = normalize_error_mode(on_error)

    try:
        row_count = len(payloads)  # type: ignore[arg-type]
    except TypeError as exc:
        raise ConfigurationError("payloads must be a sized sequence") from exc

    bitmap = coerce_null_bitmap(
        null_bitmap,
        row_count,
        bitmap_bit_order,
    )

    values: list[int | None] = []
    slots: list[bytes | None] = []
    errors: list[RowError] = []
    error_rows: list[int] = []

    for row in range(row_count):
        # Mandatory ordering: bitmap first, and continue before payloads[row].
        if bitmap.is_null(row):
            values.append(None)
            slots.append(None)
            continue

        try:
            try:
                payload = payloads[row]
            except Exception as exc:
                raise _RowConversionFailure(
                    RowError(
                        row=row,
                        code=ErrorCode.INVALID_PAYLOAD,
                        message=f"payload slot could not be read: {exc}",
                    )
                ) from exc
            value, encoded = convert_one_value(
                row,
                payload,
                source,
                target,
                accept_unsigned_sign=accept_unsigned_sign,
            )
        except _RowConversionFailure as failure:
            error = failure.error
            errors.append(error)
            error_rows.append(row)
            values.append(None)
            slots.append(None)
            continue

        values.append(value)
        slots.append(encoded)

    if errors and mode is ErrorMode.STRICT:
        # Whole-batch rejection: discard values/slots and expose all locations.
        raise BatchConversionError(errors)

    result_bitmap = bitmap.with_error_rows_null(error_rows)
    return BatchResult(
        values=tuple(values),
        slots=tuple(slots),
        errors=tuple(errors),
        null_bitmap=result_bitmap,
        target_width=target.width,
        byte_order=target.byte_order,
    )


@dataclass(frozen=True)
class ContiguousPackedColumn:
    """Fixed-stride view over one contiguous source database-page buffer.

    Slicing occurs only when the kernel requests a non-NULL row.
    """

    buffer: bytes | bytearray | memoryview
    row_count: int
    packed_width: int

    def __post_init__(self) -> None:
        if isinstance(self.row_count, bool) or not isinstance(self.row_count, int):
            raise ConfigurationError("row_count must be an integer")
        if self.row_count < 0:
            raise ConfigurationError("row_count must not be negative")
        if isinstance(self.packed_width, bool) or not isinstance(
            self.packed_width, int
        ):
            raise ConfigurationError("packed_width must be an integer")
        if self.packed_width <= 0:
            raise ConfigurationError("packed_width must be positive")

    def __len__(self) -> int:
        return self.row_count

    def __getitem__(self, row: int) -> memoryview:
        if isinstance(row, bool) or not isinstance(row, int):
            raise TypeError("row index must be an integer")
        if row < 0 or row >= self.row_count:
            raise IndexError("row index out of range")
        start = row * self.packed_width
        return memoryview(self.buffer)[start : start + self.packed_width]


def convert_packed_page(
    page: bytes | bytearray | memoryview,
    *,
    row_count: int,
    precision: int,
    source_scale: int,
    target_scale: int,
    target_width: int = 8,
    target_precision: int | None = None,
    byte_order: str = "big",
    null_bitmap: NullBitmap
    | bytes
    | bytearray
    | memoryview
    | Sequence[bool | int]
    | None = None,
    bitmap_bit_order: BitOrder | str = BitOrder.MSB,
    on_error: ErrorMode | str = ErrorMode.STRICT,
    accept_unsigned_sign: bool = False,
) -> BatchResult:
    """Convert fixed-width packed rows stored contiguously in a page buffer."""

    source = SourceColumn(precision=precision, scale=source_scale)
    column = ContiguousPackedColumn(page, row_count, source.packed_width)
    return convert_packed_batch(
        column,
        precision=precision,
        source_scale=source_scale,
        target_scale=target_scale,
        target_width=target_width,
        target_precision=target_precision,
        byte_order=byte_order,
        null_bitmap=null_bitmap,
        bitmap_bit_order=bitmap_bit_order,
        on_error=on_error,
        accept_unsigned_sign=accept_unsigned_sign,
    )
