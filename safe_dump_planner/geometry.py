"""Half-open byte interval helpers.

All ranges in this project are ``[start, end)`` byte offsets.  Half-open
ranges make boundary behaviour explicit: a field ending at offset ``n`` and a
field starting at ``n`` are adjacent, not overlapping.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from .errors import ValidationError


RangeLike = "ByteRange | Sequence[int] | dict[str, Any]"


def _as_offset(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{name} must be an integer offset, got {value!r}")
    return value


@dataclass(frozen=True, order=True)
class ByteRange:
    """An immutable ``[start, end)`` byte range."""

    start: int
    end: int

    def __post_init__(self) -> None:
        start = _as_offset(self.start, "start")
        end = _as_offset(self.end, "end")
        if start < 0:
            raise ValidationError(f"range start must be non-negative, got {start}")
        if end < start:
            raise ValidationError(f"range end {end} precedes start {start}")
        # Frozen dataclass post-init assignment needs object.__setattr__.
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)

    @property
    def length(self) -> int:
        return self.end - self.start

    def contains(self, other: "ByteRange | int") -> bool:
        if isinstance(other, int):
            return self.start <= other < self.end
        return self.start <= other.start and other.end <= self.end

    def intersects(self, other: "ByteRange") -> bool:
        return self.start < other.end and other.start < self.end

    def intersection(self, other: "ByteRange") -> "ByteRange | None":
        start = max(self.start, other.start)
        end = min(self.end, other.end)
        if end <= start:
            return None
        return ByteRange(start, end)

    def to_dict(self) -> list[int]:
        return [self.start, self.end]

    def __str__(self) -> str:
        return f"[{self.start},{self.end})"


def coerce_range(value: Any) -> ByteRange:
    """Convert a :class:`ByteRange`, tuple/list, or mapping to a range."""

    if isinstance(value, ByteRange):
        return value
    if isinstance(value, (tuple, list)):
        if len(value) != 2:
            raise ValidationError(f"byte range must contain two offsets, got {value!r}")
        return ByteRange(value[0], value[1])
    if isinstance(value, dict):
        if "start" in value and "end" in value:
            return ByteRange(value["start"], value["end"])
        if "offset" in value and ("size" in value or "length" in value):
            start = value["offset"]
            size = value.get("size", value.get("length"))
            return ByteRange(start, _as_offset(start, "offset") + _as_offset(size, "size"))
    raise ValidationError(f"invalid byte range: {value!r}")


def coerce_ranges(values: Iterable[Any] | None) -> tuple[ByteRange, ...]:
    if values is None:
        return ()
    return tuple(coerce_range(v) for v in values)


def normalize_ranges(
    values: Iterable[Any] | None,
    *,
    page_size: int | None = None,
) -> tuple[ByteRange, ...]:
    """Validate, sort, and merge overlapping or adjacent ranges.

    Zero-length ranges are dropped.  When ``page_size`` is supplied, every
    range must lie inside ``[0, page_size]``.
    """

    ranges = [coerce_range(v) for v in (values or ()) if not _is_empty_like(v)]
    if page_size is not None:
        page_size = _as_offset(page_size, "page_size")
        if page_size < 0:
            raise ValidationError("page_size must be non-negative")
        for r in ranges:
            if r.start > page_size or r.end > page_size:
                raise ValidationError(
                    f"range {r} is outside page of size {page_size}"
                )
    ranges = [r for r in ranges if r.length > 0]
    if not ranges:
        return ()
    ranges.sort()
    merged: list[ByteRange] = [ranges[0]]
    for r in ranges[1:]:
        last = merged[-1]
        if r.start <= last.end:  # overlap or adjacency
            merged[-1] = ByteRange(last.start, max(last.end, r.end))
        else:
            merged.append(r)
    return tuple(merged)


def _is_empty_like(value: Any) -> bool:
    try:
        r = coerce_range(value)
    except ValidationError:
        return False
    return r.length == 0


def union_ranges(
    left: Iterable[ByteRange] | None,
    right: Iterable[ByteRange] | None = None,
    *,
    page_size: int | None = None,
) -> tuple[ByteRange, ...]:
    values = list(left or ())
    if right:
        values.extend(right)
    return normalize_ranges(values, page_size=page_size)


def intersect_unions(
    left: Sequence[ByteRange],
    right: Sequence[ByteRange],
) -> tuple[ByteRange, ...]:
    """Intersect two normalized range unions."""

    out: list[ByteRange] = []
    i = j = 0
    while i < len(left) and j < len(right):
        item = left[i].intersection(right[j])
        if item is not None and item.length > 0:
            if out and out[-1].end == item.start:
                out[-1] = ByteRange(out[-1].start, item.end)
            else:
                out.append(item)
        if left[i].end < right[j].end:
            i += 1
        else:
            j += 1
    return tuple(out)


def subtract_unions(
    source: Sequence[ByteRange],
    removed: Sequence[ByteRange],
) -> tuple[ByteRange, ...]:
    """Return normalized ``source - removed`` for normalized range unions."""

    out: list[ByteRange] = []
    for r in source:
        cursor = r.start
        for bad in removed:
            if bad.end <= cursor:
                continue
            if bad.start >= r.end:
                break
            if bad.start > cursor:
                out.append(ByteRange(cursor, min(bad.start, r.end)))
            cursor = max(cursor, bad.end)
            if cursor >= r.end:
                break
        if cursor < r.end:
            out.append(ByteRange(cursor, r.end))
    return normalize_ranges(out)


def complement_ranges(
    allowed: Sequence[ByteRange],
    page_size: int,
) -> tuple[ByteRange, ...]:
    """Return denied gaps inside a page."""

    return subtract_unions((ByteRange(0, page_size),), allowed)


def range_covers(outer: Sequence[ByteRange], inner: ByteRange) -> bool:
    """Whether normalized ``outer`` covers every byte of ``inner``."""

    remaining_start = inner.start
    for r in outer:
        if r.end <= remaining_start:
            continue
        if r.start > remaining_start:
            return False
        remaining_start = min(r.end, inner.end)
        if remaining_start >= inner.end:
            return True
    return remaining_start >= inner.end


def uncovered_parts(
    requested: Sequence[ByteRange],
    allowed: Sequence[ByteRange],
) -> tuple[ByteRange, ...]:
    """Return requested bytes not covered by ``allowed``."""

    return subtract_unions(requested, allowed)


def elementary_boundaries(*range_sets: Iterable[ByteRange] | int) -> list[int]:
    """Collect sorted byte boundaries, including page sizes encoded as ints."""

    points: set[int] = {0}
    for item in range_sets:
        if isinstance(item, int):
            points.add(item)
        else:
            for r in item:
                points.add(r.start)
                points.add(r.end)
    return sorted(p for p in points if p >= 0)
