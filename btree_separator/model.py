"""Composite key ordering for relational index keys.

The default null position is :data:`False`, i.e. ``NULLS LAST``.  Callers can
always state ``nulls="first"`` / ``nulls_first=True`` explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Sequence


class _NegInfinity:
    """Sort-order negative infinity.

    It is smaller than ``NULL`` and every ordinary value, independently of a
    column's ascending/descending direction.  "Negative" here refers to the
    effective index sort order, not to the underlying Python value.
    """

    _instance: "_NegInfinity | None" = None

    def __new__(cls) -> "_NegInfinity":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "NEG_INFINITY"

    def __bool__(self) -> bool:  # pragma: no cover - trivial
        return False


NEG_INFINITY = _NegInfinity()


class KeyOrderError(ValueError):
    """Raised when supplied keys violate the promised leaf/order invariant."""


INTEGER_TYPES = frozenset({"int", "integer", "int64", "bigint", "long"})
TEXT_TYPES = frozenset({"text", "str", "string", "bytes", "byte", "blob", "binary"})
ROWID_TYPES = frozenset({"rowid", "row_id", "tid"})


def _normalize_type(type_name: str | None, is_rowid: bool = False) -> str:
    if is_rowid:
        return "rowid"
    if type_name is None:
        return "int64"
    normalized = str(type_name).strip().lower()
    if normalized in INTEGER_TYPES:
        return "int64"
    if normalized in TEXT_TYPES:
        return "text"
    if normalized in ROWID_TYPES:
        return "rowid"
    raise ValueError(f"unsupported column type: {type_name!r}")


@dataclass(frozen=True)
class ColumnSpec:
    """One sortable key column.

    ``nulls_first`` describes the effective composite index order and is not
    flipped by :pyattr:`descending`.  This matches the requirement that sort
    direction and NULL placement are specified independently.
    """

    name: str | None = None
    type: str = "int64"
    descending: bool = False
    nulls_first: bool = False
    is_rowid: bool = False

    def __init__(
        self,
        name: str | None = None,
        type: str | None = "int64",
        *,
        descending: bool = False,
        nulls_first: bool | None = None,
        nulls: str | bool | None = None,
        desc: bool | None = None,
        ascending: bool | None = None,
        is_rowid: bool = False,
    ) -> None:
        if desc is not None:
            descending = bool(desc)
        if ascending is not None:
            descending = not bool(ascending)

        if nulls is not None:
            if isinstance(nulls, bool):
                nulls_first = nulls
            else:
                position = str(nulls).strip().lower().replace("nulls_", "")
                if position in {"first", "front", "low"}:
                    nulls_first = True
                elif position in {"last", "back", "high"}:
                    nulls_first = False
                else:
                    raise ValueError("nulls must be 'first' or 'last'")

        if nulls_first is None:
            nulls_first = False

        object.__setattr__(self, "name", name)
        object.__setattr__(self, "type", _normalize_type(type, is_rowid))
        object.__setattr__(self, "descending", bool(descending))
        object.__setattr__(self, "nulls_first", bool(nulls_first))
        object.__setattr__(self, "is_rowid", bool(is_rowid or self.type == "rowid"))

    @property
    def ascending(self) -> bool:
        return not self.descending

    @property
    def kind(self) -> str:
        return self.type if self.type in {"int64", "text", "rowid"} else "int64"


def coerce_column(column: ColumnSpec | dict[str, Any] | str | Sequence[Any]) -> ColumnSpec:
    if isinstance(column, ColumnSpec):
        return column
    if isinstance(column, dict):
        return ColumnSpec(**column)
    if isinstance(column, str):
        return ColumnSpec(column)
    if isinstance(column, Sequence):
        # ("name", "text", True, "first") and a few convenient variants.
        args = list(column)
        name = args.pop(0) if args else None
        type_name = args.pop(0) if args else "int64"
        descending = False
        nulls_first: bool | None = None
        for item in args:
            if isinstance(item, bool):
                # Ambiguous only if two booleans are supplied; callers should
                # use keywords in that case.  The first bool is direction.
                descending = item
            elif isinstance(item, str):
                text = item.lower().replace("_", "")
                if text in {"desc", "descending"}:
                    descending = True
                elif text in {"asc", "ascending"}:
                    descending = False
                elif text in {"first", "nullsfirst"}:
                    nulls_first = True
                elif text in {"last", "nullslast"}:
                    nulls_first = False
        return ColumnSpec(
            name,
            type_name,
            descending=descending,
            nulls_first=nulls_first,
        )
    raise TypeError(f"cannot interpret column specification: {column!r}")


class KeySchema:
    """Schema for the indexed columns plus the final unique row number."""

    def __init__(
        self,
        columns: Iterable[ColumnSpec | dict[str, Any] | str | Sequence[Any]] | None = None,
        *,
        rowid_name: str = "rowid",
        rowid_descending: bool = False,
        rowid_column: ColumnSpec | None = None,
    ) -> None:
        self.user_columns: tuple[ColumnSpec, ...] = tuple(
            coerce_column(c) for c in (columns or ())
        )
        if rowid_column is not None:
            self.rowid_column = rowid_column
        else:
            self.rowid_column = ColumnSpec(
                rowid_name,
                "rowid",
                descending=rowid_descending,
                nulls_first=False,
                is_rowid=True,
            )
        self.all_columns: tuple[ColumnSpec, ...] = self.user_columns + (
            self.rowid_column,
        )
        self.size = len(self.all_columns)

    def column_at(self, index: int) -> ColumnSpec:
        return self.all_columns[index]

    def __repr__(self) -> str:
        return f"KeySchema({self.all_columns!r})"


def coerce_schema(schema: "KeySchema | Sequence[Any]") -> KeySchema:
    if isinstance(schema, KeySchema):
        return schema
    if isinstance(schema, Sequence) and not isinstance(schema, (str, bytes)):
        return KeySchema(schema)  # type: ignore[arg-type]
    raise TypeError("schema must be a KeySchema or a sequence of ColumnSpec")


class TextPrefix(bytes):
    """A raw-byte prefix used as the actual value of a text column."""

    __slots__ = ()


def text_bytes(value: Any) -> bytes:
    """Return the raw byte order representation of a text value."""
    if isinstance(value, _NegInfinity):  # pragma: no cover - guarded earlier
        raise TypeError("NEG_INFINITY is not text")
    if value is None:
        raise TypeError("NULL has no text byte representation")
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value)
    if isinstance(value, str):
        return value.encode("utf-8")
    raise TypeError(f"text/bytes column received unsupported value {value!r}")


def canonicalize_key(key: Sequence[Any], schema: KeySchema | Sequence[Any]) -> tuple[Any, ...]:
    """Convert text values to raw bytes and retain other scalar values."""
    schema = coerce_schema(schema)
    values = list(key)
    if len(values) > schema.size:
        raise KeyOrderError(
            f"key has {len(values)} values, schema has {schema.size} columns"
        )
    result: list[Any] = []
    for index, value in enumerate(values):
        column = schema.column_at(index)
        if value is NEG_INFINITY or value is None:
            result.append(value)
        elif column.kind == "text":
            result.append(text_bytes(value))
        else:
            result.append(value)
    return tuple(result)


def compare_scalar(left: Any, right: Any, column: ColumnSpec) -> int:
    """Compare two scalars in the column's effective index order."""
    left_inf = left is NEG_INFINITY
    right_inf = right is NEG_INFINITY
    if left_inf or right_inf:
        if left_inf and right_inf:
            return 0
        return -1 if left_inf else 1

    left_null = left is None
    right_null = right is None
    if left_null or right_null:
        if left_null and right_null:
            return 0
        if column.nulls_first:
            return -1 if left_null else 1
        return 1 if left_null else -1

    if column.kind == "text":
        left_value = text_bytes(left)
        right_value = text_bytes(right)
    elif column.kind in {"int64", "rowid"}:
        left_value = int(left)
        right_value = int(right)
    else:  # pragma: no cover - normalized by ColumnSpec
        raise TypeError(f"unsupported column kind {column.kind!r}")

    if left_value == right_value:
        return 0
    natural = -1 if left_value < right_value else 1
    return -natural if column.descending else natural


def _values_of(key: Any, schema: KeySchema) -> Sequence[Any]:
    # Avoid importing Separator at module import time; duck-type its comparison
    # projection.
    projection = getattr(key, "comparison_values", None)
    if callable(projection):
        return projection()
    return key


def compare_keys(
    left: Any,
    right: Any,
    schema: KeySchema | Sequence[Any],
) -> int:
    """Lexicographically compare composite keys.

    A missing trailing component is :data:`NEG_INFINITY`.  This is also the
    comparison used for truncated separators.
    """
    schema = coerce_schema(schema)
    left_values = _values_of(left, schema)
    right_values = _values_of(right, schema)

    width = max(schema.size, len(left_values), len(right_values))
    for index in range(width):
        column = schema.column_at(index) if index < schema.size else None
        if column is None:
            # Keys wider than the declared schema have no defined order.  All
            # supported callers use the schema width, so this is defensive.
            raise KeyOrderError("key component lies beyond schema columns")
        a = left_values[index] if index < len(left_values) else NEG_INFINITY
        b = right_values[index] if index < len(right_values) else NEG_INFINITY
        result = compare_scalar(a, b, column)
        if result:
            return result
    return 0


def keys_equal(
    left: Sequence[Any],
    right: Sequence[Any],
    schema: KeySchema | Sequence[Any],
) -> bool:
    return compare_keys(left, right, schema) == 0


def is_sorted(
    keys: Sequence[Sequence[Any]],
    schema: KeySchema | Sequence[Any],
    *,
    strict: bool = False,
) -> bool:
    schema = coerce_schema(schema)
    for previous, current in zip(keys, keys[1:]):
        relation = compare_keys(previous, current, schema)
        if relation > 0 or (strict and relation == 0):
            return False
    return True


def validate_sorted_keys(
    keys: Sequence[Sequence[Any]],
    schema: KeySchema | Sequence[Any],
    *,
    strict: bool = True,
) -> None:
    schema = coerce_schema(schema)
    for index, (previous, current) in enumerate(zip(keys, keys[1:])):
        relation = compare_keys(previous, current, schema)
        if relation > 0:
            raise KeyOrderError(f"key at index {index} is greater than the next key")
        if strict and relation == 0:
            raise KeyOrderError(f"duplicate key at index {index + 1}")
