"""Paragraph-range primitives.

All ranges in this package are 1-based and inclusive.  This matches the way
reviewers normally cite paragraph numbers in a statement or interview record.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence


@dataclass(frozen=True, order=True)
class Range:
    """An inclusive paragraph range."""

    start: int
    end: int

    def __post_init__(self) -> None:
        if not isinstance(self.start, int) or not isinstance(self.end, int):
            raise TypeError("range endpoints must be integers")
        if self.start < 1 or self.end < 1:
            raise ValueError("paragraph numbers are 1-based")
        if self.start > self.end:
            raise ValueError(f"invalid range: {self.start} > {self.end}")

    @classmethod
    def point(cls, number: int) -> "Range":
        return cls(number, number)

    @classmethod
    def from_value(cls, value: "Range | Sequence[int] | int") -> "Range":
        if isinstance(value, Range):
            return value
        if isinstance(value, int):
            return Range.point(value)
        if len(value) == 2:
            return Range(int(value[0]), int(value[1]))
        raise ValueError(f"cannot parse range: {value!r}")

    @property
    def length(self) -> int:
        return self.end - self.start + 1

    def numbers(self) -> range:
        return range(self.start, self.end + 1)

    def contains_number(self, number: int) -> bool:
        return self.start <= number <= self.end

    def contains(self, other: "Range") -> bool:
        return self.start <= other.start and other.end <= self.end

    def overlaps(self, other: "Range") -> bool:
        return self.start <= other.end and other.start <= self.end

    def intersection(self, other: "Range") -> "Range | None":
        start = max(self.start, other.start)
        end = min(self.end, other.end)
        if start > end:
            return None
        return Range(start, end)

    def is_adjacent_to(self, other: "Range") -> bool:
        return self.end + 1 == other.start or other.end + 1 == self.start

    def subtract(self, cuts: Iterable["Range"]) -> list["Range"]:
        """Return the parts of this range not covered by ``cuts``."""

        remaining: list[Range] = [self]
        for cut in normalize_ranges(cuts):
            next_remaining: list[Range] = []
            for part in remaining:
                intersection = part.intersection(cut)
                if intersection is None:
                    next_remaining.append(part)
                    continue
                if part.start < intersection.start:
                    next_remaining.append(Range(part.start, intersection.start - 1))
                if intersection.end < part.end:
                    next_remaining.append(Range(intersection.end + 1, part.end))
            remaining = normalize_ranges(next_remaining)
        return remaining

    def to_list(self) -> list[int]:
        return [self.start, self.end]

    def __str__(self) -> str:
        if self.start == self.end:
            return str(self.start)
        return f"{self.start}-{self.end}"


def as_ranges(values: Range | Sequence[Range | Sequence[int] | int] | int | None) -> list[Range]:
    if not values:
        return []
    if isinstance(values, Range):
        return [values]
    if isinstance(values, int):
        return [Range.point(values)]
    return [Range.from_value(value) for value in values]


def normalize_ranges(ranges: Iterable[Range]) -> list[Range]:
    """Merge overlapping or adjacent ranges into maximal ranges."""

    ordered = sorted(ranges)
    merged: list[Range] = []
    for current in ordered:
        if not merged:
            merged.append(current)
            continue
        previous = merged[-1]
        if current.start <= previous.end + 1:
            merged[-1] = Range(previous.start, max(previous.end, current.end))
        else:
            merged.append(current)
    return merged


def intersect_many(left: Iterable[Range], right: Iterable[Range]) -> list[Range]:
    result: list[Range] = []
    for a in left:
        for b in right:
            intersection = a.intersection(b)
            if intersection is not None:
                result.append(intersection)
    return normalize_ranges(result)


def ranges_to_numbers(ranges: Iterable[Range]) -> set[int]:
    numbers: set[int] = set()
    for paragraph_range in ranges:
        numbers.update(paragraph_range.numbers())
    return numbers


def group_numbers_into_ranges(numbers: Iterable[int]) -> list[Range]:
    ordered = sorted(set(numbers))
    if not ordered:
        return []
    ranges: list[Range] = []
    start = previous = ordered[0]
    for number in ordered[1:]:
        if number == previous + 1:
            previous = number
            continue
        ranges.append(Range(start, previous))
        start = previous = number
    ranges.append(Range(start, previous))
    return ranges
