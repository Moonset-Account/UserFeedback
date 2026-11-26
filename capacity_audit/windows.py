"""Fixed half-open windows and clock-aware window assignment."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterator

from .clock import TimeBounds, _finite_number


@dataclass(frozen=True)
class MetricBounds:
    """Integer count bounds (or floating throughput bounds)."""

    lower: float
    point: float
    upper: float

    def __post_init__(self) -> None:
        lo = _finite_number(self.lower, "lower")
        point = _finite_number(self.point, "point")
        upper = _finite_number(self.upper, "upper")
        if not (lo <= upper + 1e-9):
            raise ValueError(f"expected lower <= upper, got {self!r}")
        # Correct tiny floating-point ordering noise.
        point = min(max(point, lo), upper)
        object.__setattr__(self, "lower", lo)
        object.__setattr__(self, "point", point)
        object.__setattr__(self, "upper", upper)

    @classmethod
    def zero(cls) -> "MetricBounds":
        return cls(0, 0, 0)

    def add_certain(self, value: float) -> "MetricBounds":
        return MetricBounds(self.lower + value, self.point + value, self.upper + value)

    def add_possible_only(self, value: float) -> "MetricBounds":
        return MetricBounds(self.lower, self.point, self.upper + value)

    def add_point_only(self, value: float) -> "MetricBounds":
        return MetricBounds(self.lower, self.point + value, self.upper + value)

    def scale(self, factor: float) -> "MetricBounds":
        return MetricBounds(
            self.lower * factor,
            self.point * factor,
            self.upper * factor,
        )

    def to_dict(self) -> dict[str, float]:
        return {"lower": self.lower, "point": self.point, "upper": self.upper}


@dataclass(frozen=True)
class Window:
    index: int
    start: float
    end: float

    @property
    def width(self) -> float:
        return self.end - self.start

    def contains_exact(self, timestamp: float) -> bool:
        return self.start <= timestamp < self.end

    def to_dict(self) -> dict[str, float | int]:
        return {"index": self.index, "start": self.start, "end": self.end, "width": self.width}


class WindowGrid:
    """Fixed-width windows ``[origin + i*width, origin + (i+1)*width)``."""

    def __init__(self, width: float = 10.0, origin: float = 0.0) -> None:
        width = _finite_number(width, "width")
        origin = _finite_number(origin, "origin")
        if width <= 0:
            raise ValueError("window_width must be positive")
        self.width = width
        self.origin = origin

    def index_at(self, timestamp: float) -> int:
        ts = _finite_number(timestamp, "timestamp")
        return math.floor((ts - self.origin) / self.width)

    def window(self, index: int) -> Window:
        start = self.origin + index * self.width
        # Compute end from index, not start+width, to reduce cumulative drift.
        end = self.origin + (index + 1) * self.width
        return Window(index=index, start=start, end=end)

    def window_at(self, timestamp: float) -> Window:
        return self.window(self.index_at(timestamp))

    def possible_indices(self, bounds: TimeBounds) -> list[int]:
        first = self.index_at(bounds.lower)
        last = self.index_at(bounds.upper)
        return list(range(first, last + 1))

    def certain_index(self, bounds: TimeBounds) -> int | None:
        indices = self.possible_indices(bounds)
        if len(indices) != 1:
            return None
        index = indices[0]
        window = self.window(index)
        # Closed time interval [lower, upper] is certainly inside a half-open
        # window only when upper is strictly before the excluded end.
        if bounds.lower >= window.start and bounds.upper < window.end:
            return index
        return None

    def assignment(self, bounds: TimeBounds) -> "WindowAssignment":
        possible = self.possible_indices(bounds)
        certain = self.certain_index(bounds)
        point = self.index_at(bounds.point)
        return WindowAssignment(point_index=point, certain_index=certain, possible_indices=possible)

    def range_for_time(self, start_time: float, end_time: float) -> list[Window]:
        first = self.index_at(start_time)
        last = self.index_at(end_time)
        return [self.window(i) for i in range(first, last + 1)]

    def windows_between(self, lower_time: float, upper_time: float) -> list[Window]:
        return self.range_for_time(lower_time, upper_time)


@dataclass(frozen=True)
class WindowAssignment:
    point_index: int
    certain_index: int | None
    possible_indices: list[int]

    def to_dict(self) -> dict[str, object]:
        return {
            "point_index": self.point_index,
            "certain_index": self.certain_index,
            "possible_indices": list(self.possible_indices),
        }
