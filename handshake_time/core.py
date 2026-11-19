"""UTC handshake time adjudication.

The module models every authenticated time receipt as a *closed* UTC
interval plus the clock source that produced it.  A receipt is one claim:
"the true time is somewhere inside this interval".  Copies of a claim from
the same clock source are the same vote, so they are unioned per source
before votes are counted.

An instant is retained when at least ``min_sources`` distinct sources cover
it.  The retained set is therefore a union of separated closed intervals.
An interval bridge between two retained pieces is never invented: gaps stay
gaps.  Certificate validity is evaluated only against retained instants.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Sequence

__all__ = [
    "AdjudicationResult",
    "TimeReceipt",
    "UtcInterval",
    "Verdict",
    "adjudicate",
    "covers",
    "retained_segments",
]


def _as_utc(value: datetime) -> datetime:
    """Return an aware datetime in UTC, rejecting naive datetimes."""
    if not isinstance(value, datetime):
        raise TypeError(f"expected datetime, got {type(value).__name__}")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware (UTC timestamps required)")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class UtcInterval:
    """A closed UTC interval ``[start, end]``."""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        start = _as_utc(self.start)
        end = _as_utc(self.end)
        if start > end:
            raise ValueError(f"interval start {start.isoformat()} is after end {end.isoformat()}")
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)

    def contains(self, moment: datetime) -> bool:
        moment = _as_utc(moment)
        return self.start <= moment <= self.end

    def to_dict(self) -> dict[str, str]:
        return {"start": _iso_z(self.start), "end": _iso_z(self.end)}


@dataclass(frozen=True)
class TimeReceipt:
    """An authenticated claim that true UTC time lies in ``[start, end]``."""

    start: datetime
    end: datetime
    source: str
    authenticated: bool = True

    def __post_init__(self) -> None:
        interval = UtcInterval(self.start, self.end)
        object.__setattr__(self, "start", interval.start)
        object.__setattr__(self, "end", interval.end)

        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("receipt source must be a non-empty string")
        object.__setattr__(self, "source", self.source.strip())

        if not isinstance(self.authenticated, bool):
            raise TypeError("authenticated must be a bool")

    @property
    def interval(self) -> UtcInterval:
        return UtcInterval(self.start, self.end)

    @property
    def clock_source(self) -> str:
        return self.source


class Verdict(str, enum.Enum):
    """Outcome of handshake verification."""

    RELEASE = "release"
    REJECT = "reject"
    HOLD = "hold"


@dataclass(frozen=True)
class AdjudicationResult:
    """Retained time evidence and the certificate decision derived from it."""

    verdict: Verdict
    retained: tuple[UtcInterval, ...]
    valid_segments: tuple[UtcInterval, ...]
    fully_valid: bool
    fully_invalid: bool
    ignored_unauthenticated: int
    reason: str

    @property
    def trusted_segments(self) -> tuple[UtcInterval, ...]:
        """Alias for ``retained`` (the segments supported by enough sources)."""

        return self.retained

    def to_dict(self) -> dict[str, object]:
        return {
            "verdict": self.verdict.value,
            "retained": [segment.to_dict() for segment in self.retained],
            "valid": [segment.to_dict() for segment in self.valid_segments],
            "fully_valid": self.fully_valid,
            "fully_invalid": self.fully_invalid,
            "ignored_unauthenticated": self.ignored_unauthenticated,
            "reason": self.reason,
        }


def _iso_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _union_source_intervals(intervals: Iterable[UtcInterval]) -> list[UtcInterval]:
    """Union all intervals of one source, merging touching closed intervals."""

    ordered = sorted(intervals, key=lambda iv: (iv.start, iv.end))
    merged: list[UtcInterval] = []
    for interval in ordered:
        if merged and interval.start <= merged[-1].end:
            if interval.end > merged[-1].end:
                merged[-1] = UtcInterval(merged[-1].start, interval.end)
        else:
            merged.append(interval)
    return merged


def _intersection(left: UtcInterval, right: UtcInterval) -> UtcInterval | None:
    start = max(left.start, right.start)
    end = min(left.end, right.end)
    if start > end:
        return None
    return UtcInterval(start, end)


def _merge_pieces(pieces: Iterable[UtcInterval]) -> tuple[UtcInterval, ...]:
    """Merge overlapping or touching closed pieces into maximal segments."""

    ordered = sorted(pieces, key=lambda iv: (iv.start, iv.end))
    merged: list[UtcInterval] = []
    for piece in ordered:
        if merged and piece.start <= merged[-1].end:
            if piece.end > merged[-1].end:
                merged[-1] = UtcInterval(merged[-1].start, piece.end)
        else:
            merged.append(piece)
    return tuple(merged)


def _validate_min_sources(min_sources: int) -> None:
    if isinstance(min_sources, bool) or not isinstance(min_sources, int):
        raise TypeError("min_sources must be an integer")
    if min_sources < 1:
        raise ValueError("min_sources must be at least 1")


def _retained_from_grouped(
    grouped: dict[str, Sequence[UtcInterval]],
    min_sources: int,
) -> tuple[UtcInterval, ...]:
    """Compute instants covered by at least ``min_sources`` distinct sources.

    Only union intervals have to be considered: every endpoint of an input
    interval is an endpoint of the union, so coverage is constant on each
    open slab between consecutive endpoints.
    """

    if not grouped:
        return ()

    points = sorted(
        {interval.start for intervals in grouped.values() for interval in intervals}
        | {interval.end for intervals in grouped.values() for interval in intervals}
    )

    def point_support(point: datetime) -> int:
        return sum(
            1
            for intervals in grouped.values()
            if any(interval.start <= point <= interval.end for interval in intervals)
        )

    def slab_support(left: datetime, right: datetime) -> int:
        return sum(
            1
            for intervals in grouped.values()
            if any(interval.start <= left and interval.end >= right for interval in intervals)
        )

    pieces: list[UtcInterval] = []
    for index, point in enumerate(points):
        if index + 1 < len(points):
            right = points[index + 1]
            if slab_support(point, right) >= min_sources:
                # The whole closed slab is supported, endpoints included.
                pieces.append(UtcInterval(point, right))
                continue
        if point_support(point) >= min_sources:
            # An isolated instant can qualify even when both adjacent slabs do not.
            pieces.append(UtcInterval(point, point))

    return _merge_pieces(pieces)


def retained_segments(
    receipts: Iterable[TimeReceipt],
    *,
    min_sources: int,
) -> tuple[UtcInterval, ...]:
    """Return separated closed segments supported by enough distinct sources.

    Unauthenticated receipts are ignored.  Receipts from the same source are
    unioned first, so any number of same-source copies still cast at most one
    vote at any instant.
    """

    _validate_min_sources(min_sources)

    grouped: dict[str, list[UtcInterval]] = {}
    for receipt in receipts:
        if not isinstance(receipt, TimeReceipt):
            raise TypeError(f"expected TimeReceipt, got {type(receipt).__name__}")
        if not receipt.authenticated:
            continue
        grouped.setdefault(receipt.source, []).append(receipt.interval)

    unified = {source: _union_source_intervals(intervals) for source, intervals in grouped.items()}
    return _retained_from_grouped(unified, min_sources)


def adjudicate(
    receipts: Iterable[TimeReceipt],
    *,
    min_sources: int,
    valid_from: datetime,
    valid_to: datetime,
) -> AdjudicationResult:
    """Decide whether the handshake may proceed.

    ``valid_from``/``valid_to`` are the caller-supplied closed certificate
    validity period.  The decision considers only retained instants; any gap
    between retained segments is outside the calculation.

    * ``RELEASE``: retained instants exist and all lie inside validity.
    * ``REJECT``: retained instants exist and none lie inside validity.
    * ``HOLD``: no retained instant (insufficient evidence) or a mixture of
      valid and invalid retained instants.
    """

    _validate_min_sources(min_sources)
    validity = UtcInterval(valid_from, valid_to)

    materialized = list(receipts)
    ignored = 0
    for receipt in materialized:
        if not isinstance(receipt, TimeReceipt):
            raise TypeError(f"expected TimeReceipt, got {type(receipt).__name__}")
        if not receipt.authenticated:
            ignored += 1

    retained = retained_segments(materialized, min_sources=min_sources)

    if not retained:
        return AdjudicationResult(
            verdict=Verdict.HOLD,
            retained=(),
            valid_segments=(),
            fully_valid=False,
            fully_invalid=False,
            ignored_unauthenticated=ignored,
            reason="insufficient evidence: no instant reached the required distinct-source count",
        )

    valid_parts: list[UtcInterval] = []
    fully_valid = True
    fully_invalid = True
    for segment in retained:
        overlap = _intersection(segment, validity)
        if overlap is not None:
            valid_parts.append(overlap)
            fully_invalid = False
        if not (validity.start <= segment.start and segment.end <= validity.end):
            fully_valid = False

    valid_segments = _merge_pieces(valid_parts)

    if fully_valid:
        verdict = Verdict.RELEASE
        reason = "all retained instants are inside certificate validity"
    elif fully_invalid:
        verdict = Verdict.REJECT
        reason = "all retained instants are outside certificate validity"
    else:
        verdict = Verdict.HOLD
        reason = "retained instants mix valid and invalid certificate periods"

    return AdjudicationResult(
        verdict=verdict,
        retained=retained,
        valid_segments=valid_segments,
        fully_valid=fully_valid,
        fully_invalid=fully_invalid,
        ignored_unauthenticated=ignored,
        reason=reason,
    )


def covers(segments: Iterable[UtcInterval], moment: datetime) -> bool:
    """Return whether any retained segment contains ``moment`` (closed)."""

    return any(segment.contains(moment) for segment in segments)
