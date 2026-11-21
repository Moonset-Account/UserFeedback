"""Version vectors and integer interval constraints.

A version is a tuple of non-negative integers (a "version vector").  A
constraint is a set of closed integer intervals over one component of that
vector; several constraints on the same package are combined by
intersection.  This is deliberately small and total so every comparison is
deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence, Tuple

Version = Tuple[int, ...]


def normalise_version(value: Sequence[int]) -> Version:
    """Return a hashable, fully-integer version vector."""
    try:
        parts = tuple(int(part) for part in value)
    except (TypeError, ValueError) as exc:  # pragma: no cover - defensive
        raise ValueError(f"version must be a sequence of integers, got {value!r}") from exc
    if not parts:
        raise ValueError("version vector must contain at least one component")
    if any(part < 0 for part in parts):
        raise ValueError(f"version components must be non-negative, got {parts!r}")
    return parts


def version_key(version: Version) -> Version:
    """Comparison key.

    Lexicographic order over the integer vector.  Vectors of different length
    compare element by element; a shorter prefix sorts before the longer
    vector that extends it.
    """
    return tuple(version)


def compare_versions(left: Version, right: Version) -> int:
    """Return -1, 0 or 1 for the lexicographic comparison of two vectors."""
    left_key = version_key(left)
    right_key = version_key(right)
    if left_key < right_key:
        return -1
    if left_key > right_key:
        return 1
    return 0


@dataclass(frozen=True)
class Interval:
    """A closed integer interval ``[low, high]``.

    ``None`` bounds mean unbounded on that side.  An empty interval is
    represented by ``low > high`` (or a bound pair that is impossible) and is
    rejected at construction time via :meth:`is_empty`.
    """

    low: Optional[int] = None
    high: Optional[int] = None

    def __post_init__(self) -> None:
        if self.low is not None and self.high is not None and self.low > self.high:
            raise ValueError(
                f"empty interval [{self.low}, {self.high}]: low must not exceed high"
            )

    @classmethod
    def exact(cls, value: int) -> "Interval":
        return cls(value, value)

    @classmethod
    def at_least(cls, value: int) -> "Interval":
        return cls(value, None)

    @classmethod
    def at_most(cls, value: int) -> "Interval":
        return cls(None, value)

    @classmethod
    def any(cls) -> "Interval":
        return cls(None, None)

    def contains(self, value: int) -> bool:
        if self.low is not None and value < self.low:
            return False
        if self.high is not None and value > self.high:
            return False
        return True

    def intersect(self, other: "Interval") -> Optional["Interval"]:
        """Return the intersection, or ``None`` when the intervals are disjoint."""
        low_candidates = [bound for bound in (self.low, other.low) if bound is not None]
        high_candidates = [bound for bound in (self.high, other.high) if bound is not None]
        low = max(low_candidates) if low_candidates else None
        high = min(high_candidates) if high_candidates else None
        if low is not None and high is not None and low > high:
            return None
        return Interval(low, high)

    def describe(self) -> str:
        low = "-inf" if self.low is None else str(self.low)
        high = "+inf" if self.high is None else str(self.high)
        return f"[{low}, {high}]"


@dataclass(frozen=True)
class VersionRange:
    """A conjunction of integer interval constraints on one version component.

    Coordinates are 0-based positions into the version vector.  Several
    :class:`Interval` entries for the same coordinate are intersected in
    order; a coord with an empty intersection makes the whole range empty.
    """

    components: Tuple[Tuple[int, Interval], ...]

    def __post_init__(self) -> None:
        # Normalise to a sorted tuple of (coord, interval) pairs so equality
        # and hashing are stable regardless of construction order.
        merged: dict[int, Interval] = {}
        for coord, interval in self.components:
            if coord < 0:
                raise ValueError(f"component coordinate must be >= 0, got {coord}")
            if not isinstance(interval, Interval):
                raise TypeError("range components must be Interval instances")
            current = merged.get(coord)
            merged[coord] = interval if current is None else _require(
                current.intersect(interval),
                f"conflicting component ranges at coordinate {coord}",
            )
        object.__setattr__(
            self,
            "components",
            tuple(sorted(merged.items(), key=lambda item: item[0])),
        )

    @classmethod
    def any(cls) -> "VersionRange":
        return cls(())

    @classmethod
    def at_least(cls, coord: int, value: int) -> "VersionRange":
        return cls(((coord, Interval.at_least(value)),))

    @classmethod
    def at_most(cls, coord: int, value: int) -> "VersionRange":
        return cls(((coord, Interval.at_most(value)),))

    @classmethod
    def exact(cls, coord: int, value: int) -> "VersionRange":
        return cls(((coord, Interval.exact(value)),))

    def intersect(self, other: "VersionRange") -> Optional["VersionRange"]:
        merged = dict(self.components)
        for coord, interval in other.components:
            current = merged.get(coord)
            combined = interval if current is None else current.intersect(interval)
            if combined is None:
                return None
            merged[coord] = combined
        return VersionRange(tuple(sorted(merged.items(), key=lambda item: item[0])))

    def allows(self, version: Version) -> bool:
        for coord, interval in self.components:
            if coord >= len(version):
                # A constraint on a coordinate the version does not carry is
                # treated as unsatisfied only when it excludes the implicit 0
                # padding used by ``pad_version``.
                if not interval.contains(0):
                    return False
                continue
            if not interval.contains(version[coord]):
                return False
        return True

    def describe(self) -> str:
        if not self.components:
            return "any"
        parts = []
        for coord, interval in self.components:
            parts.append(f"v{coord}{interval.describe()}")
        return " & ".join(parts)


def _require(value, message):
    if value is None:
        raise ValueError(message)
    return value
