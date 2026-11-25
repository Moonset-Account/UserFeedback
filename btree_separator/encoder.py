"""Enumerate right-minimum prefixes and choose the shortest separator encoding."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .model import (
    NEG_INFINITY,
    ColumnSpec,
    KeyOrderError,
    KeySchema,
    TextPrefix,
    canonicalize_key,
    coerce_schema,
    compare_keys,
    text_bytes,
)

# Public one-byte marker values.  The numerical values are an implementation
# format defined by this package; only their one-byte cost is specified by the
# problem statement.
TAG_NEG_INFINITY = 0x00
TAG_NULL = 0x01
TAG_INT64 = 0x02
TAG_TEXT = 0x03
TAG_ROWID = 0x04


@dataclass(frozen=True)
class EncodedColumn:
    role: str
    tag: int
    payload: bytes
    column_index: int | None = None

    @property
    def size(self) -> int:
        """Specified cost: one marker byte plus payload."""
        return 1 + len(self.payload)


@dataclass(frozen=True)
class Separator:
    """A candidate or selected separator.

    ``values`` contains actual encoded column values (including
    :class:`TextPrefix` values).  When ``terminal_negative_infinity`` is true,
    comparison appends :data:`NEG_INFINITY`; the encoding also contains a
    zero-payload negative-infinity marker column.
    """

    values: tuple[Any, ...]
    terminal_negative_infinity: bool
    kind: str
    encoded_columns: tuple[EncodedColumn, ...]
    order: int = 0
    right_minimum: tuple[Any, ...] = field(default=(), compare=False)

    @property
    def byte_length(self) -> int:
        return sum(column.size for column in self.encoded_columns)

    @property
    def length(self) -> int:
        return self.byte_length

    @property
    def size(self) -> int:
        return self.byte_length

    @property
    def encoded_size(self) -> int:
        return self.byte_length

    @property
    def columns(self) -> tuple[EncodedColumn, ...]:
        return self.encoded_columns

    @property
    def encoded_column_count(self) -> int:
        """Number of one-byte markers, including a terminal -infinity marker."""
        return len(self.encoded_columns)

    @property
    def value_column_count(self) -> int:
        """Number of real key columns represented before the terminal marker."""
        return len(self.values)

    @property
    def column_count(self) -> int:
        """Alias used by the tie-break: number of encoded columns/markers."""
        return self.encoded_column_count

    def comparison_values(self) -> tuple[Any, ...]:
        if self.terminal_negative_infinity:
            return self.values + (NEG_INFINITY,)
        return self.values

    # The model's duck typing accepts either a callable or a sequence; expose
    # the projected tuple as a property too for convenient interactive use.
    @property
    def key_values(self) -> tuple[Any, ...]:
        return self.comparison_values()

    def to_bytes(self) -> bytes:
        """Concatenate marker and payload bytes.

        Text payloads are raw bytes and deliberately have no extra length field:
        the problem's cost model is marker + payload only.  Routing therefore
        remains schema/candidate aware rather than pretending this compact
        image is independently self-describing.
        """
        return b"".join(
            bytes((column.tag,)) + column.payload for column in self.encoded_columns
        )

    def hex(self) -> str:
        return self.to_bytes().hex(" ")

    def is_valid_for(self, left_max: Sequence[Any], right_min: Sequence[Any],
                     schema: KeySchema | Sequence[Any]) -> bool:
        schema = coerce_schema(schema)
        left_max = canonicalize_key(left_max, schema)
        right_min = canonicalize_key(right_min, schema)
        separator_key = self.comparison_values()
        return (
            compare_keys(left_max, separator_key, schema) < 0
            and compare_keys(separator_key, right_min, schema) <= 0
        )

    def explain(self) -> str:
        parts = [
            f"{column.role}@{column.column_index}:{column.size}B"
            for column in self.encoded_columns
        ]
        return (
            f"{self.kind} length={self.byte_length} "
            f"columns={self.encoded_column_count} [{', '.join(parts)}]"
        )


def encode_column(value: Any, column: ColumnSpec, index: int) -> EncodedColumn:
    if value is NEG_INFINITY:
        return EncodedColumn("negative_infinity", TAG_NEG_INFINITY, b"", index)
    if value is None:
        return EncodedColumn("null", TAG_NULL, b"", index)

    if column.kind == "text":
        payload = text_bytes(value)
        return EncodedColumn("text_prefix_or_value", TAG_TEXT, payload, index)

    if column.kind == "rowid":
        number = int(value)
        if number < 0:
            # Row numbers in this index model are positive/non-negative.
            try:
                payload = number.to_bytes(8, byteorder="big", signed=True)
            except OverflowError as exc:  # pragma: no cover - defensive
                raise KeyOrderError("rowid must fit in eight bytes") from exc
        else:
            try:
                payload = number.to_bytes(8, byteorder="big", signed=False)
            except OverflowError as exc:  # pragma: no cover - defensive
                raise KeyOrderError("rowid must fit in unsigned eight bytes") from exc
        return EncodedColumn("rowid", TAG_ROWID, payload, index)

    # Integers are fixed at eight bytes as required.
    number = int(value)
    try:
        payload = number.to_bytes(8, byteorder="big", signed=True)
    except OverflowError as exc:  # pragma: no cover - defensive
        raise KeyOrderError("integer must fit in signed eight bytes") from exc
    return EncodedColumn("integer", TAG_INT64, payload, index)


def make_separator(
    values: Sequence[Any],
    schema: KeySchema,
    *,
    terminal_negative_infinity: bool,
    kind: str,
    order: int = 0,
    right_minimum: Sequence[Any] = (),
) -> Separator:
    encoded = [
        encode_column(value, schema.column_at(index), index)
        for index, value in enumerate(values)
    ]
    if terminal_negative_infinity:
        encoded.append(
            EncodedColumn(
                "negative_infinity",
                TAG_NEG_INFINITY,
                b"",
                len(values),
            )
        )
    return Separator(
        values=tuple(values),
        terminal_negative_infinity=terminal_negative_infinity,
        kind=kind,
        encoded_columns=tuple(encoded),
        order=order,
        right_minimum=tuple(right_minimum),
    )


def prefix_candidates(
    right_min: Sequence[Any],
    schema: KeySchema | Sequence[Any],
) -> list[Separator]:
    """Enumerate every allowed prefix candidate of ``right_min``.

    Candidates are:

    * ``R[:k]`` followed by a sort-order negative-infinity marker (k < arity);
    * the complete right minimum (k == arity);
    * for each non-NULL text column, every raw-byte prefix as that column's
      actual value, followed by negative infinity if it is not the last column.

    Equal representations (notably a complete empty/full text prefix and a
    column boundary) are de-duplicated.
    """
    schema = coerce_schema(schema)
    right = canonicalize_key(right_min, schema)
    if len(right) != schema.size:
        raise KeyOrderError(
            f"right minimum must contain all {schema.size} key columns/rowid"
        )

    candidates: list[Separator] = []
    seen: set[tuple[tuple[Any, ...], bool]] = set()
    order = 0

    def add(values: Sequence[Any], terminal: bool, kind: str) -> None:
        nonlocal order
        candidate = make_separator(
            values,
            schema,
            terminal_negative_infinity=terminal,
            kind=kind,
            order=order,
            right_minimum=right,
        )
        identity = (candidate.values, candidate.terminal_negative_infinity)
        if identity in seen:
            return
        seen.add(identity)
        candidates.append(candidate)
        order += 1

    # Column-boundary prefixes, including the zero-column -infinity candidate
    # and the complete right key.
    for k in range(schema.size + 1):
        add(
            right[:k],
            k < schema.size,
            "column_boundary" if k < schema.size else "full_right_key",
        )

    # Raw byte prefixes inside text columns.  The rowid is never text.
    for column_index, column in enumerate(schema.all_columns):
        if column.kind != "text" or right[column_index] is None:
            continue
        raw = text_bytes(right[column_index])
        for prefix_length in range(len(raw) + 1):
            prefix_value = TextPrefix(raw[:prefix_length])
            values = right[:column_index] + (prefix_value,)
            # Text is a user column, and the trailing rowid means a terminal
            # sentinel is normally present.  Keep the general condition anyway.
            terminal = column_index + 1 < schema.size
            add(values, terminal, "text_byte_prefix")

    return candidates


def valid_separators(
    left_max: Sequence[Any],
    right_min: Sequence[Any],
    schema: KeySchema | Sequence[Any],
    *,
    candidates: Iterable[Separator] | None = None,
) -> list[Separator]:
    schema = coerce_schema(schema)
    left = canonicalize_key(left_max, schema)
    right = canonicalize_key(right_min, schema)
    if len(left) != schema.size or len(right) != schema.size:
        raise KeyOrderError("left and right boundary keys must be complete keys")
    if compare_keys(left, right, schema) >= 0:
        raise KeyOrderError("left maximum must be strictly below right minimum")

    source = list(candidates) if candidates is not None else prefix_candidates(right, schema)
    valid: list[Separator] = []
    for candidate in source:
        key = candidate.comparison_values()
        if compare_keys(left, key, schema) < 0 and compare_keys(key, right, schema) <= 0:
            valid.append(candidate)
    return valid


def separator_sort_key(candidate: Separator) -> tuple[int, int, int, int]:
    """Primary shortest byte length, then fewer encoded columns."""
    return (
        candidate.byte_length,
        candidate.encoded_column_count,
        candidate.value_column_count,
        candidate.order,
    )


def choose_separator(
    left_max: Sequence[Any],
    right_min: Sequence[Any],
    schema: KeySchema | Sequence[Any],
) -> Separator:
    """Return the shortest valid right-prefix separator.

    Ties are resolved by fewer encoded columns and then deterministic
    enumeration order.
    """
    valid = valid_separators(left_max, right_min, schema)
    if not valid:  # pragma: no cover - full R is always valid for L < R
        raise KeyOrderError("no valid separator candidate")
    return min(valid, key=separator_sort_key)


# The module's primary encoder API uses the problem statement's wording.
encode_separator = choose_separator


# Descriptive aliases for callers that expect encoder-oriented naming.
class SeparatorEncoder:
    def __init__(self, schema: KeySchema | Sequence[Any]) -> None:
        self.schema = coerce_schema(schema)

    def candidates(self, right_min: Sequence[Any]) -> list[Separator]:
        return prefix_candidates(right_min, self.schema)

    def encode(
        self,
        left_max: Sequence[Any],
        right_min: Sequence[Any],
    ) -> Separator:
        return choose_separator(left_max, right_min, self.schema)

    def encode_column(self, value: Any, index: int) -> EncodedColumn:
        return encode_column(value, self.schema.column_at(index), index)
