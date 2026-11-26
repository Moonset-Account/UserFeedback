"""Interval algebra used by the offline transfer approval engine.

The module intentionally supports both inclusive and exclusive endpoints.
JSON/config inputs default to closed intervals because duty hand-over windows
are normally quoted with their boundary instants included.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Iterator, Sequence


class IntervalTypeError(TypeError):
    """Raised when interval endpoints cannot be compared as time values."""


@dataclass(frozen=True)
class Interval:
    """A possibly endpoint-open time interval.

    Empty intervals are represented by ``empty=True``.  The ordinary
    constructor turns degenerate endpoint-open intervals such as ``[t, t)``
    into empty intervals.
    """

    start: Any = None
    end: Any = None
    start_inclusive: bool = True
    end_inclusive: bool = True
    empty: bool = False

    def __post_init__(self) -> None:
        if self.empty:
            object.__setattr__(self, "start", None)
            object.__setattr__(self, "end", None)
            object.__setattr__(self, "start_inclusive", True)
            object.__setattr__(self, "end_inclusive", True)
            return
        if self.start is None or self.end is None:
            raise ValueError("non-empty Interval requires start and end")
        try:
            ordered = self.start <= self.end
        except TypeError as exc:  # pragma: no cover - defensive boundary
            raise IntervalTypeError("interval endpoints are not comparable") from exc
        if not ordered:
            raise ValueError("Interval start must not be after end")
        if self.start == self.end and (
            not self.start_inclusive or not self.end_inclusive
        ):
            object.__setattr__(self, "empty", True)
            object.__setattr__(self, "start", None)
            object.__setattr__(self, "end", None)
            object.__setattr__(self, "start_inclusive", True)
            object.__setattr__(self, "end_inclusive", True)

    @classmethod
    def point(cls, value: Any) -> "Interval":
        return cls(value, value, True, True)

    @classmethod
    def empty_interval(cls) -> "Interval":
        return cls(empty=True)

    @property
    def is_empty(self) -> bool:
        return self.empty

    def contains(self, value: Any) -> bool:
        if self.empty:
            return False
        try:
            if value < self.start or value > self.end:
                return False
            if value == self.start and not self.start_inclusive:
                return False
            if value == self.end and not self.end_inclusive:
                return False
        except TypeError as exc:
            raise IntervalTypeError("value is not comparable with interval") from exc
        return True

    def intersection(self, other: "Interval") -> "Interval":
        return intersect(self, other)

    def intersects(self, other: "Interval") -> bool:
        return not intersect(self, other).empty

    def to_dict(self) -> dict[str, Any]:
        if self.empty:
            return {"empty": True}
        return {
            "start": self.start,
            "end": self.end,
            "start_inclusive": self.start_inclusive,
            "end_inclusive": self.end_inclusive,
        }

    def __bool__(self) -> bool:
        return not self.empty


def _check_pair(a: Interval, b: Interval) -> None:
    if not isinstance(a, Interval) or not isinstance(b, Interval):
        raise TypeError("expected Interval objects")


def intersect(a: Interval, b: Interval) -> Interval:
    """Return the set intersection of two intervals."""

    _check_pair(a, b)
    if a.empty or b.empty:
        return Interval.empty_interval()

    try:
        lo = max(a.start, b.start)
        hi = min(a.end, b.end)
    except TypeError as exc:
        raise IntervalTypeError("cannot compare interval endpoints") from exc

    if lo > hi:
        return Interval.empty_interval()

    lo_inclusive = True
    if a.start == lo:
        lo_inclusive = lo_inclusive and a.start_inclusive
    if b.start == lo:
        lo_inclusive = lo_inclusive and b.start_inclusive

    hi_inclusive = True
    if a.end == hi:
        hi_inclusive = hi_inclusive and a.end_inclusive
    if b.end == hi:
        hi_inclusive = hi_inclusive and b.end_inclusive

    result = Interval(lo, hi, lo_inclusive, hi_inclusive)
    return Interval.empty_interval() if result.empty else result


def intersect_all(intervals: Iterable[Interval]) -> Interval:
    result: Interval | None = None
    for interval in intervals:
        result = interval if result is None else intersect(result, interval)
        if result.empty:
            return Interval.empty_interval()
    if result is None:
        raise ValueError("intersect_all requires at least one interval")
    return result


def add_intervals(a: Interval, b: Interval) -> Interval:
    """Minkowski sum; useful for exact delay projections."""

    _check_pair(a, b)
    if a.empty or b.empty:
        return Interval.empty_interval()
    return Interval(
        a.start + b.start,
        a.end + b.end,
        a.start_inclusive and b.start_inclusive,
        a.end_inclusive and b.end_inclusive,
    )


def shift(interval: Interval, lower_delta: Any, upper_delta: Any | None = None) -> Interval:
    if interval.empty:
        return Interval.empty_interval()
    if upper_delta is None:
        upper_delta = lower_delta
    return Interval(
        interval.start + lower_delta,
        interval.end + upper_delta,
        interval.start_inclusive,
        interval.end_inclusive,
    )


def _sort_key(interval: Interval) -> tuple[Any, int]:
    # At the same lower bound, a closed lower endpoint starts first.
    return (interval.start, 0 if interval.start_inclusive else 1)


def merge_intervals(intervals: Iterable[Interval]) -> list[Interval]:
    """Merge overlapping or point-connected intervals.

    Two intervals sharing only an excluded endpoint do not merge, because the
    shared instant is absent from their union.
    """

    items = [iv for iv in intervals if isinstance(iv, Interval) and not iv.empty]
    if not items:
        return []
    items.sort(key=_sort_key)

    merged: list[Interval] = []
    for item in items:
        if not merged:
            merged.append(item)
            continue

        current = merged[-1]
        overlap = intersect(current, item)
        connected = False
        if overlap.empty and current.end == item.start:
            connected = current.end_inclusive or item.start_inclusive

        if not overlap.empty or connected:
            if item.end > current.end:
                end, end_inclusive = item.end, item.end_inclusive
            elif item.end == current.end:
                end = current.end
                end_inclusive = current.end_inclusive or item.end_inclusive
            else:
                end = current.end
                end_inclusive = current.end_inclusive
            replacement = Interval(
                current.start,
                end,
                current.start_inclusive,
                end_inclusive,
            )
            merged[-1] = replacement
        else:
            merged.append(item)

    return merged


def subtract(a: Interval, b: Interval) -> list[Interval]:
    """Return ``a - b`` as zero, one, or two intervals."""

    _check_pair(a, b)
    if a.empty:
        return []
    if b.empty or intersect(a, b).empty:
        return [a]

    pieces: list[Interval] = []

    if a.start <= b.start:
        left_end_inclusive = not b.start_inclusive
        left = Interval(
            a.start,
            b.start,
            a.start_inclusive,
            left_end_inclusive,
        )
        if not left.empty:
            pieces.append(left)

    if b.end <= a.end:
        right_start_inclusive = not b.end_inclusive
        right = Interval(
            b.end,
            a.end,
            right_start_inclusive,
            a.end_inclusive,
        )
        if not right.empty:
            pieces.append(right)

    return merge_intervals(pieces)


def subtract_many(base: Interval, removed: Iterable[Interval]) -> list[Interval]:
    pieces = [base] if not base.empty else []
    for removal in removed:
        next_pieces: list[Interval] = []
        for piece in pieces:
            next_pieces.extend(subtract(piece, removal))
        pieces = merge_intervals(next_pieces)
        if not pieces:
            break
    return pieces


def union_minus_intersection(intervals: Sequence[Interval]) -> list[Interval]:
    """Parts covered by at least one input but not by every input."""

    valid = [iv for iv in intervals if not iv.empty]
    if not valid:
        return []
    union = merge_intervals(valid)
    common = intersect_all(valid)
    if common.empty:
        return union
    result: list[Interval] = []
    for part in union:
        result.extend(subtract(part, common))
    return merge_intervals(result)


def sample_point(interval: Interval) -> Any:
    """Return one point contained in a non-empty interval.

    Closed boundaries are preferred so integer test fixtures remain stable.
    For an open boundary on a continuous numeric domain, the midpoint is used.
    """

    if interval.empty:
        raise ValueError("cannot sample an empty interval")
    if interval.start_inclusive:
        return interval.start
    if interval.end_inclusive:
        return interval.end
    if interval.start == interval.end:  # pragma: no cover - normalized as empty
        raise ValueError("cannot sample excluded degenerate interval")
    try:
        return (interval.start + interval.end) / 2
    except TypeError as exc:  # pragma: no cover - non-numeric custom time
        raise IntervalTypeError(
            "open interval requires numeric endpoints for sampling"
        ) from exc


def intervals_to_dicts(intervals: Iterable[Interval]) -> list[dict[str, Any]]:
    return [iv.to_dict() for iv in intervals]
