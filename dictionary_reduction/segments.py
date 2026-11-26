"""Input segment/shard representation and marker normalization."""

from __future__ import annotations

from collections.abc import Sequence
import operator
from typing import Any

from .dictionary import code_mapping
from .errors import (
    DictionaryStructureError,
    MeasureError,
    NullMarkerError,
    RowIdentifierError,
)
from .ordering import OrderDeclaration


def normalize_null_markers(markers: Any, length: int) -> tuple[bool, ...]:
    """Normalize null markers.

    Accepted forms:

    * ``None``: no nulls;
    * a sequence of bool/0/1 (or ``None`` entries meaning null);
    * a non-negative integer bitmask, bit ``i`` is null when set;
    * ``bytes``/``bytearray`` as a packed LSB-first bitmap.
    """

    if length < 0:
        raise NullMarkerError("length must be non-negative")

    if markers is None:
        return (False,) * length

    if isinstance(markers, bool):
        return (markers,) * length

    if isinstance(markers, int):
        if markers < 0:
            raise NullMarkerError("integer null bitmask must be non-negative")
        return tuple(bool((markers >> i) & 1) for i in range(length))

    if isinstance(markers, (bytes, bytearray)):
        result: list[bool] = []
        for i in range(length):
            byte_index, bit_index = divmod(i, 8)
            bit = (markers[byte_index] >> bit_index) & 1 if byte_index < len(markers) else 0
            result.append(bool(bit))
        return tuple(result)

    if isinstance(markers, str):
        raise NullMarkerError(
            "null markers cannot be a string; pass a sequence of bool/0/1"
        )

    try:
        marker_list = list(markers)
    except TypeError as exc:
        raise NullMarkerError(
            "null markers must be a sequence, integer bitmask, or None"
        ) from exc

    if len(marker_list) != length:
        raise NullMarkerError(
            f"expected {length} null markers, got {len(marker_list)}"
        )

    result = []
    for i, marker in enumerate(marker_list):
        if marker is None:
            result.append(True)
        elif isinstance(marker, bool):
            result.append(marker)
        elif isinstance(marker, int) and marker in (0, 1):
            result.append(bool(marker))
        else:
            raise NullMarkerError(
                f"null marker at position {i} must be bool/0/1/None, "
                f"got {marker!r}"
            )
    return tuple(result)


def _normalize_integer_column(
    values: Any, length: int, field_name: str, *, default: int | None
) -> tuple[int | None, ...]:
    if values is None:
        if default is None:
            raise MeasureError(f"{field_name} is required")
        return (default,) * length

    if isinstance(values, (str, bytes, bytearray)):
        raise MeasureError(f"{field_name} must be a sequence of integers")

    try:
        raw = list(values)
    except TypeError as exc:
        raise MeasureError(f"{field_name} must be a sequence of integers") from exc

    if len(raw) != length:
        raise MeasureError(
            f"expected {length} {field_name}, got {len(raw)}"
        )

    result: list[int | None] = []
    for i, value in enumerate(raw):
        if value is None:
            result.append(None)
            continue
        try:
            result.append(operator.index(value))
        except TypeError as exc:
            raise MeasureError(
                f"{field_name}[{i}] must be an integer or None, got {value!r}"
            ) from exc
    return tuple(result)


def normalize_row_ids(
    row_ids: Any,
    start_row_id: int | None,
    length: int,
) -> tuple[int, ...]:
    if row_ids is not None:
        if isinstance(row_ids, (str, bytes, bytearray)):
            raise RowIdentifierError("row_ids must be a sequence of integers")
        try:
            raw = list(row_ids)
        except TypeError as exc:
            raise RowIdentifierError("row_ids must be a sequence of integers") from exc
        if len(raw) != length:
            raise RowIdentifierError(
                f"expected {length} row ids, got {len(raw)}"
            )
        result: list[int] = []
        for i, value in enumerate(raw):
            try:
                row_id = operator.index(value)
            except TypeError as exc:
                raise RowIdentifierError(
                    f"row_ids[{i}] must be an integer, got {value!r}"
                ) from exc
            result.append(row_id)
        return tuple(result)

    start = 0 if start_row_id is None else start_row_id
    try:
        start = operator.index(start)
    except TypeError as exc:
        raise RowIdentifierError("start_row_id must be an integer") from exc
    return tuple(start + i for i in range(length))


class DictionarySegment:
    """One input shard/segment encoded with its own local dictionary.

    The local dictionary is deliberately namespaced by this object.  A code
    such as ``0`` is never interpreted relative to another segment.
    """

    def __init__(
        self,
        codes: Sequence[int | None],
        dictionary: Any = (),
        *,
        nulls: Any = None,
        measures: Any = None,
        values: Any = None,
        amounts: Any = None,
        row_ids: Any = None,
        global_rows: Any = None,
        start_row_id: int | None = None,
        start_row: int | None = None,
        segment_id: str | None = None,
        shard_id: str | None = None,
        ordering: OrderDeclaration | None = None,
        order: OrderDeclaration | None = None,
        domain: Any = None,
    ) -> None:
        try:
            raw_codes = list(codes)
        except TypeError as exc:
            raise DictionaryStructureError("codes must be a sequence of integers") from exc

        self.row_count = len(raw_codes)
        self.dictionary: dict[int, Any] = code_mapping(dictionary)
        self.nulls = normalize_null_markers(nulls, self.row_count)

        chosen_measures = measures
        if chosen_measures is None:
            chosen_measures = values
        if chosen_measures is None:
            chosen_measures = amounts
        self.measures = _normalize_integer_column(
            chosen_measures,
            self.row_count,
            "measures",
            default=0,
        )

        chosen_rows = row_ids if row_ids is not None else global_rows
        chosen_start = start_row_id if start_row_id is not None else start_row
        self.row_ids = normalize_row_ids(
            chosen_rows, chosen_start, self.row_count
        )

        chosen_order = ordering if ordering is not None else order
        if chosen_order is not None and not isinstance(chosen_order, OrderDeclaration):
            raise TypeError("ordering must be an OrderDeclaration or None")
        self.ordering = chosen_order
        self.domain = domain

        self.segment_id = segment_id if segment_id is not None else shard_id
        self.shard_id = shard_id if shard_id is not None else segment_id

        normalized_codes: list[int] = []
        for i, code in enumerate(raw_codes):
            if self.nulls[i]:
                # The null marker is authoritative; the code is don't-care
                # and may be an out-of-range sentinel or None.
                normalized_codes.append(-1)
                continue
            if code is None:
                from .errors import CodeOutOfRangeError

                raise CodeOutOfRangeError(
                    f"non-null row {i} has None dictionary code"
                )
            try:
                normalized_codes.append(operator.index(code))
            except TypeError as exc:
                from .errors import CodeOutOfRangeError

                raise CodeOutOfRangeError(
                    f"codes[{i}] must be an integer, got {code!r}"
                ) from exc
        self.codes: tuple[int, ...] = tuple(normalized_codes)

    def decoded_values(self, domain: Any | None = None) -> tuple[Any, ...]:
        """Decode all rows, using None for null rows."""

        result: list[Any] = []
        for i, code in enumerate(self.codes):
            if self.nulls[i]:
                result.append(None)
                continue
            if code not in self.dictionary:
                from .errors import CodeOutOfRangeError

                raise CodeOutOfRangeError(
                    f"segment {self.segment_id!r} row {i} references missing "
                    f"local dictionary code {code}"
                )
            value = self.dictionary[code]
            if domain is not None:
                domain.check_value(value)
            result.append(value)
        return tuple(result)

    def verify_order(self, domain: Any) -> "DictionarySegment":
        """Verify this segment's order declaration against decoded data."""

        from .ordering import validate_order_declaration

        values = self.decoded_values(domain)
        validate_order_declaration(
            values=values,
            nulls=self.nulls,
            declaration=self.ordering,
            domain=domain,
            row_ids=self.row_ids,
        )
        return self

    def __repr__(self) -> str:
        return (
            "DictionarySegment("
            f"id={self.segment_id!r}, rows={self.row_count}, "
            f"dictionary_size={len(self.dictionary)})"
        )


# Vocabulary aliases.
Shard = DictionarySegment
DictionaryShard = DictionarySegment
Segment = DictionarySegment
