"""Half-open time interval primitives used by the evidence accounting module.

All intervals are represented as ``(start, end)`` tuples with ``start < end``
and are interpreted as half-open ranges ``[start, end)``.  Time is expressed
in a single common unit (typically epoch seconds, possibly fractional).
"""

from __future__ import annotations

from collections.abc import Sequence

Interval = tuple[float, float]


def intersect(a: Interval, b: Interval) -> Interval | None:
    """Return the overlap of two half-open intervals, or ``None``."""
    lo = max(a[0], b[0])
    hi = min(a[1], b[1])
    return (lo, hi) if lo < hi else None


def intersect_lists(
    intervals: Sequence[Interval], bounds: Sequence[Interval]
) -> list[Interval]:
    """Intersect every interval with every bound and keep positive overlaps."""
    out: list[Interval] = []
    for iv in intervals:
        for bound in bounds:
            overlap = intersect(iv, bound)
            if overlap is not None:
                out.append(overlap)
    return merge(out)


def subtract(a: Interval, b: Interval) -> list[Interval]:
    """Return the parts of interval ``a`` not covered by interval ``b``."""
    # b entirely covers a
    if b[0] <= a[0] and a[1] <= b[1]:
        return []
    # b is disjoint from a
    if b[1] <= a[0] or a[1] <= b[0]:
        return [a]
    # b covers the left part
    if b[0] <= a[0] < b[1] < a[1]:
        return [(b[1], a[1])]
    # b covers the right part
    if a[0] < b[0] < a[1] and a[1] <= b[1]:
        return [(a[0], b[0])]
    # b is strictly inside a, splitting it in two
    return [(a[0], b[0]), (b[1], a[1])]


def subtract_many(interval: Interval, cuts: Sequence[Interval]) -> list[Interval]:
    """Subtract every cut interval from ``interval``."""
    pieces = [interval]
    for cut in cuts:
        pieces = [
            part
            for current in pieces
            for part in subtract(current, cut)
        ]
    return pieces


def merge(intervals: Sequence[Interval]) -> list[Interval]:
    """Merge overlapping or touching half-open intervals."""
    ordered = sorted(iv for iv in intervals if iv[0] < iv[1])
    if not ordered:
        return []
    merged: list[Interval] = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def merge_with_blocked(
    intervals: Sequence[Interval], blocked_points: Sequence[float]
) -> list[Interval]:
    """Merge intervals but never merge across a blocked instant.

    A blocked instant inside an interval splits that interval at the instant;
    two intervals that only touch at a blocked instant are kept separate.
    """
    blocked = sorted(set(blocked_points))
    pieces: list[Interval] = []
    for start, end in merge(intervals):
        cuts = [point for point in blocked if start < point < end]
        cursor = start
        for point in cuts:
            pieces.append((cursor, point))
            cursor = point
        pieces.append((cursor, end))
    # Merge touching pieces, except when the touching point is blocked.
    pieces = sorted(pieces)
    if not pieces:
        return []
    merged: list[Interval] = [pieces[0]]
    blocked_set = set(blocked)
    for start, end in pieces[1:]:
        last_start, last_end = merged[-1]
        if start == last_end and start not in blocked_set:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return [iv for iv in merged if iv[0] < iv[1]]


def uncovered_parts(
    span: Interval, covered: Sequence[Interval]
) -> list[Interval]:
    """Return the parts of ``span`` not covered by ``covered``."""
    covered = merge(covered)
    remaining = [span]
    for cov in covered:
        remaining = [
            part
            for current in remaining
            for part in subtract(current, cov)
        ]
    return remaining


def earliest_gap(
    span: Interval,
    covered: Sequence[Interval],
    blocked_points: Sequence[float] = (),
) -> float | None:
    """Return the earliest uncovered instant inside ``span``.

    A blocked instant counts as uncovered even when the surrounding intervals
    cover both of its sides.  Returns ``None`` when the whole span (excluding
    any blocked instants) is covered and no blocked instant exists.
    """
    gaps = uncovered_parts(span, covered)
    candidates = [gap[0] for gap in gaps if gap[0] < gap[1]]
    blocked_inside = [
        point for point in blocked_points if span[0] <= point < span[1]
    ]
    candidates.extend(blocked_inside)
    return min(candidates) if candidates else None
