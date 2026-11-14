"""Stable failure classifications and the per-record failure record."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

# Stable classification strings (documented in README.md):
# file level
INPUT_UNREADABLE = "input_unreadable"
INPUT_SIZE_LIMIT = "input_size_limit_exceeded"
INVALID_ENCODING = "invalid_encoding"
# record level
RECORD_SIZE_LIMIT = "record_size_limit_exceeded"
INVALID_JSON = "invalid_json"
RECORD_SHAPE = "record_shape_invalid"
DEPTH_LIMIT = "depth_limit_exceeded"
LIST_ITEM_LIMIT = "list_item_limit_exceeded"
DUPLICATE_FIELD = "duplicate_field"
UNSUPPORTED_VALUE = "unsupported_value"
MISSING_FIELD = "missing_field"
TYPE_ERROR = "type_error"
INVALID_VALUE = "invalid_value"
UNKNOWN_FIELD = "unknown_field"
UNKNOWN_EXTENSION = "unknown_extension"
UNREGISTERED_VERSION = "unregistered_version"
UNREGISTERED_TASK_TYPE = "unregistered_task_type"
UNREGISTERED_COMBINATION = "unregistered_combination"
RECORD_DECODE_ERROR = "record_decode_error"


@dataclass
class ImportFailure(Exception):
    """One classified import problem.

    ``field`` is the JSONPath-like location in the offending record
    (``$`` means the record root).  ``line`` is the 1-based source line.
    """

    classification: str
    field: str = "$"
    detail: str = ""
    line: Optional[int] = None
    record_id: Optional[str] = None
    column: Optional[int] = None

    def __post_init__(self) -> None:
        Exception.__init__(self)

    def __str__(self) -> str:
        return f"{self.classification} at {self.field}: {self.detail}"

    def to_dict(self, include_location: bool = True) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if include_location and self.line is not None:
            out["line"] = self.line
        if include_location and self.record_id is not None:
            out["record_id"] = self.record_id
        out["classification"] = self.classification
        out["field"] = self.field
        if include_location and self.column is not None:
            out["column"] = self.column
        out["detail"] = self.detail
        return out


class LimitsConfigError(Exception):
    """Raised when a ``--limits`` JSON configuration file is unusable."""
