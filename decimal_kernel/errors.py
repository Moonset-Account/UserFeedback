"""Errors and row-level diagnostics for the packed DECIMAL conversion kernel."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class ErrorCode(str, Enum):
    """Stable, machine-readable conversion error categories."""

    INVALID_PAYLOAD_LENGTH = "invalid_payload_length"
    INVALID_PAYLOAD = "invalid_payload"
    INVALID_DIGIT_NIBBLE = "invalid_digit_nibble"
    INVALID_SIGN_NIBBLE = "invalid_sign_nibble"
    PRECISION_OVERFLOW = "precision_overflow"
    FIXED_WIDTH_OVERFLOW = "fixed_width_overflow"


class ConversionKernelError(Exception):
    """Base class for all errors raised by this package."""


class ConfigurationError(ConversionKernelError, ValueError):
    """The caller supplied an invalid column, bitmap, or option."""


class BitmapError(ConfigurationError):
    """A null bitmap is truncated or incompatible with the row count."""


@dataclass(frozen=True)
class RowError:
    """A data error located at a batch row and, when applicable, a nibble."""

    row: int
    code: ErrorCode
    message: str
    byte_offset: int | None = None
    nibble_index: int | None = None
    bad_nibble: int | None = None
    rounded_value: int | None = None
    limit: int | None = None
    stage: str | None = None

    @property
    def is_overflow(self) -> bool:
        return self.code in (
            ErrorCode.PRECISION_OVERFLOW,
            ErrorCode.FIXED_WIDTH_OVERFLOW,
        )


class BatchConversionError(ConversionKernelError):
    """Raised in strict mode when one or more rows are invalid.

    Strict mode never returns a partial :class:`~decimal_kernel.api.BatchResult`;
    the complete set of located row errors is available on ``errors``.
    """

    def __init__(self, errors: list[RowError] | tuple[RowError, ...]):
        self.errors: tuple[RowError, ...] = tuple(errors)
        if not self.errors:
            raise ValueError("BatchConversionError requires at least one error")
        self.first_error = self.errors[0]
        self.rows: tuple[int, ...] = tuple(error.row for error in self.errors)
        super().__init__(
            f"strict conversion rejected batch; {len(self.errors)} row error(s) "
            f"at rows {list(self.rows)}"
        )


class RowConversionException(ConversionKernelError):
    """Exception carrying a located :class:`RowError` for one bad row."""

    def __init__(self, error: RowError):
        super().__init__(error.message)
        self.error = error
        self.row = error.row


# Internal alias used by row-level decoding helpers.
_RowConversionFailure = RowConversionException


def row_error_to_dict(error: RowError) -> dict[str, Any]:
    """Return a JSON-ish dictionary for logs or caller-side error tables."""

    data = {
        "row": error.row,
        "code": error.code.value,
        "message": error.message,
    }
    for key in (
        "byte_offset",
        "nibble_index",
        "bad_nibble",
        "rounded_value",
        "limit",
        "stage",
    ):
        value = getattr(error, key)
        if value is not None:
            data[key] = value
    data["is_overflow"] = error.is_overflow
    return data
