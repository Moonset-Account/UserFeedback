"""Controllable clock and clock-uncertainty primitives.

The analyzer deliberately separates the *reported* client timestamp from the
*reference-time interval* implied by a known offset and error bound.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Union


Number = Union[int, float]


def _finite_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number, got {value!r}")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite, got {value!r}")
    return result


@dataclass(frozen=True)
class TimeBounds:
    """Possible reference-time interval for one event.

    The possible true time is the closed interval ``[lower, upper]``; ``point``
    is the corrected point estimate (reported timestamp plus offset).
    """

    lower: float
    point: float
    upper: float
    offset: float = 0.0
    error_bound: float = 0.0

    def __post_init__(self) -> None:
        lo = _finite_number(self.lower, "lower")
        point = _finite_number(self.point, "point")
        hi = _finite_number(self.upper, "upper")
        error = _finite_number(self.error_bound, "error_bound")
        offset = _finite_number(self.offset, "offset")
        if error < 0:
            raise ValueError("error_bound must be non-negative")
        if not (lo <= point <= hi):
            raise ValueError(f"invalid time bounds: [{lo}, {point}, {hi}]")
        object.__setattr__(self, "lower", lo)
        object.__setattr__(self, "point", point)
        object.__setattr__(self, "upper", hi)
        object.__setattr__(self, "error_bound", error)
        object.__setattr__(self, "offset", offset)

    @property
    def width(self) -> float:
        return self.upper - self.lower

    def duration_bounds_to(self, end: "TimeBounds | float") -> "MetricRange":
        """Return min/max elapsed duration to another bounded endpoint.

        Causal ordering makes a negative queue duration impossible.  Clock
        intervals may overlap, in which case the defensible lower bound is zero.
        """

        if isinstance(end, TimeBounds):
            end_lo = end.lower
            end_point = end.point
            end_hi = end.upper
        else:
            end_lo = end_point = end_hi = _finite_number(end, "end")
        minimum = max(0.0, end_lo - self.upper)
        point = max(0.0, end_point - self.point)
        maximum = max(0.0, end_hi - self.lower)
        return MetricRange(lower=minimum, point=point, upper=maximum)


@dataclass(frozen=True)
class MetricRange:
    lower: float
    point: float
    upper: float

    def __post_init__(self) -> None:
        lo = _finite_number(self.lower, "lower")
        point = _finite_number(self.point, "point")
        upper = _finite_number(self.upper, "upper")
        if lo > upper + 1e-9:
            raise ValueError("metric range requires lower <= upper")
        point = min(max(point, lo), upper)
        object.__setattr__(self, "lower", lo)
        object.__setattr__(self, "point", point)
        object.__setattr__(self, "upper", upper)


@dataclass(frozen=True)
class ClockSpec:
    """Clock correction for one client.

    Reference time is estimated as ``reported + offset``.  The true reference
    time is bounded by ``point +/- error_bound``.
    """

    offset: float = 0.0
    error_bound: float = 0.0

    def __post_init__(self) -> None:
        offset = _finite_number(self.offset, "offset")
        error = _finite_number(self.error_bound, "error_bound")
        if error < 0:
            raise ValueError("error_bound must be non-negative")
        object.__setattr__(self, "offset", offset)
        object.__setattr__(self, "error_bound", error)

    def correct(self, reported_timestamp: Number) -> TimeBounds:
        ts = _finite_number(reported_timestamp, "reported_timestamp")
        point = ts + self.offset
        return TimeBounds(
            lower=point - self.error_bound,
            point=point,
            upper=point + self.error_bound,
            offset=self.offset,
            error_bound=self.error_bound,
        )


# Descriptive alias used in some APIs/tests.
ClockBounds = ClockSpec


class ManualClock:
    """A deterministic clock controlled by the test/analysis driver."""

    def __init__(self, start: Number = 0.0) -> None:
        self._now = _finite_number(start, "start")

    @property
    def now(self) -> float:
        return self._now

    def time(self) -> float:
        return self._now

    def stamp(self) -> float:
        return self._now

    def advance(self, seconds: Number) -> float:
        delta = _finite_number(seconds, "seconds")
        if delta < 0:
            raise ValueError("ManualClock cannot move backwards with advance(); use set()")
        self._now += delta
        return self._now

    # Test-friendly alias.
    sleep = advance

    def set(self, timestamp: Number) -> float:
        self._now = _finite_number(timestamp, "timestamp")
        return self._now
