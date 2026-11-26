"""Order declarations for dictionary-coded segments."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from .errors import OrderDeclarationError

_ASCENDING = {"asc", "ascending", "increasing", "1", 1}
_DESCENDING = {"desc", "descending", "decreasing", "-1", -1}
_UNORDERED = {"any", "none", "unordered", "unsorted", None, 0}
_NULL_FIRST = {"first", "low", "minimum", "start"}
_NULL_LAST = {"last", "high", "maximum", "end"}


def _normalize_direction(value: Any, *, field_name: str) -> str:
    if isinstance(value, str):
        normalized = value.strip().lower().replace("-", "_")
        if normalized in {"asc", "ascending", "increasing"}:
            return "ascending"
        if normalized in {"desc", "descending", "decreasing"}:
            return "descending"
        if normalized in {"any", "none", "unordered", "unsorted"}:
            return "unordered"
    if value == 1:
        return "ascending"
    if value == -1:
        return "descending"
    if value == 0 or value is None:
        return "unordered"
    raise OrderDeclarationError(f"invalid {field_name}: {value!r}")


def _normalize_nulls(value: Any, direction: str) -> str:
    if value is None:
        return "last" if direction == "ascending" else "first"
    if isinstance(value, str):
        normalized = value.strip().lower().replace("-", "_")
        if normalized in _NULL_FIRST or normalized == "nulls_first":
            return "first"
        if normalized in _NULL_LAST or normalized == "nulls_last":
            return "last"
    raise OrderDeclarationError(f"invalid null placement: {value!r}")


@dataclass(frozen=True)
class OrderDeclaration:
    """Declared ordering for one segment.

    Attributes
    ----------
    direction:
        ``"ascending"``, ``"descending"``, or ``"unordered"`` for values.
    nulls:
        ``"first"`` or ``"last"``.  Defaults to last for ascending and first
        for descending.
    rows:
        Declaration of global row-number order within the segment:
        ``"ascending"``, ``"descending"``, or ``"any"``.  This is metadata;
        aggregation always takes the minimum global row number regardless of
        processing or declaration order.
    equal_adjacent:
        If true, rows in the same domain group must be contiguous.
    """

    direction: str = "unordered"
    nulls: str | None = None
    rows: str = "any"
    equal_adjacent: bool = False

    def __post_init__(self) -> None:
        direction = _normalize_direction(self.direction, field_name="direction")
        rows = _normalize_direction(self.rows, field_name="rows")
        nulls = _normalize_nulls(self.nulls, direction)
        if nulls not in {"first", "last"}:
            raise OrderDeclarationError(f"invalid null placement: {nulls!r}")
        object.__setattr__(self, "direction", direction)
        object.__setattr__(self, "rows", rows)
        object.__setattr__(self, "nulls", nulls)

    @classmethod
    def ascending(
        cls, *, nulls: str | None = "last", rows: str = "any", equal_adjacent: bool = False
    ) -> "OrderDeclaration":
        return cls("ascending", nulls, rows, equal_adjacent)

    @classmethod
    def descending(
        cls, *, nulls: str | None = "first", rows: str = "any", equal_adjacent: bool = False
    ) -> "OrderDeclaration":
        return cls("descending", nulls, rows, equal_adjacent)

    @classmethod
    def unordered(cls, *, rows: str = "any") -> "OrderDeclaration":
        return cls("unordered", None, rows, False)

    @property
    def multiplier(self) -> int:
        if self.direction == "ascending":
            return 1
        if self.direction == "descending":
            return -1
        return 0


def validate_order_declaration(
    *,
    values: Sequence[Any],
    nulls: Sequence[bool],
    declaration: OrderDeclaration | None,
    domain: Any,
    row_ids: Sequence[int] | None = None,
) -> None:
    """Validate decoded values and/or global row ids against a declaration."""

    if declaration is None:
        return

    n = len(values)
    if len(nulls) != n:
        raise OrderDeclarationError("null marker length does not match values")

    if declaration.direction != "unordered" and n >= 2:
        for i in range(1, n):
            previous_null = bool(nulls[i - 1])
            current_null = bool(nulls[i])

            if previous_null and current_null:
                continue
            if previous_null and not current_null:
                if declaration.nulls != "first":
                    raise OrderDeclarationError(
                        f"null at row {i - 1} precedes non-null at row {i}, "
                        "but declaration uses nulls=last"
                    )
                continue
            if not previous_null and current_null:
                if declaration.nulls != "last":
                    raise OrderDeclarationError(
                        f"non-null at row {i - 1} precedes null at row {i}, "
                        "but declaration uses nulls=first"
                    )
                continue

            cmp_value = domain.compare(values[i - 1], values[i])
            if declaration.direction == "ascending" and cmp_value > 0:
                raise OrderDeclarationError(
                    "values are not ascending at positions "
                    f"{i - 1} and {i}: {values[i - 1]!r} > {values[i]!r}"
                )
            if declaration.direction == "descending" and cmp_value < 0:
                raise OrderDeclarationError(
                    "values are not descending at positions "
                    f"{i - 1} and {i}: {values[i - 1]!r} < {values[i]!r}"
                )

    if declaration.equal_adjacent and declaration.direction != "unordered":
        seen_groups: set[Any] = set()
        previous_key: Any = object()
        for i, (value, is_null) in enumerate(zip(values, nulls)):
            key = None if is_null else domain.group_key(value)
            if key != previous_key:
                if key in seen_groups:
                    raise OrderDeclarationError(
                        f"group {key!r} is not contiguous at row {i}"
                    )
                seen_groups.add(key)
                previous_key = key

    if declaration.rows != "any" and row_ids is not None and len(row_ids) >= 2:
        direction = declaration.rows
        for i in range(1, len(row_ids)):
            left, right = row_ids[i - 1], row_ids[i]
            if direction == "ascending" and left > right:
                raise OrderDeclarationError(
                    "global row ids are not ascending at positions "
                    f"{i - 1} and {i}: {left} > {right}"
                )
            if direction == "descending" and left < right:
                raise OrderDeclarationError(
                    "global row ids are not descending at positions "
                    f"{i - 1} and {i}: {left} < {right}"
                )


SortDeclaration = OrderDeclaration
SegmentOrder = OrderDeclaration
Ordering = OrderDeclaration
