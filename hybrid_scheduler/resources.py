"""Immutable multi-dimensional resource vectors.

The scheduler intentionally treats execution slots and memory as named
dimensions of one vector.  Additional dimensions (for example GPU slots or
fast memory) can be added without changing the planner.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Iterable


Number = int | float


def _normalize(data: Mapping[str, Number] | Iterable[tuple[str, Number]]) -> tuple[tuple[str, Number], ...]:
    if isinstance(data, Mapping):
        items = list(data.items())
    else:
        items = list(data)

    normalized: dict[str, Number] = {}
    for key, value in items:
        name = str(key)
        if not name:
            raise ValueError("resource dimension names must be non-empty")
        if isinstance(value, bool):
            # bool is an int subclass; reject it to avoid surprising declarations.
            raise TypeError(f"resource value for {name!r} must be a real number, not bool")
        if not isinstance(value, (int, float)):
            raise TypeError(f"resource value for {name!r} must be a number, got {type(value)!r}")
        if value < 0:
            raise ValueError(f"resource value for {name!r} must be non-negative, got {value}")
        if name in normalized:
            normalized[name] += value
        else:
            normalized[name] = value
    # Drop zero components so {"memory": 0} and {} have the same hash/equality.
    return tuple(sorted((key, value) for key, value in normalized.items() if value != 0))


class ResourceVector:
    """An immutable, non-negative resource vector.

    Missing dimensions compare as zero.  All arithmetic returns a new vector;
    instances are safe to share between planning snapshots and events.
    """

    __slots__ = ("_items", "_map", "_hash")

    def __init__(
        self,
        values: Mapping[str, Number] | Iterable[tuple[str, Number]] | None = None,
        **kwargs: Number,
    ) -> None:
        data: dict[str, Number] = {}
        if values is not None:
            if isinstance(values, Mapping):
                data.update(values)
            else:
                for key, value in values:
                    data[key] = data.get(key, 0) + value
        data.update(kwargs)
        items = _normalize(data)
        object.__setattr__(self, "_items", items)
        object.__setattr__(self, "_map", dict(items))
        object.__setattr__(self, "_hash", hash(items))

    @classmethod
    def of(cls, slots: Number = 0, memory: Number = 0, **dimensions: Number) -> "ResourceVector":
        """Create a vector using the two domain-standard dimensions."""
        dimensions["slots"] = slots
        dimensions["memory"] = memory
        return cls(dimensions)

    @classmethod
    def zero(cls) -> "ResourceVector":
        return ZERO_VECTOR

    @property
    def dimensions(self) -> tuple[str, ...]:
        return tuple(key for key, _ in self._items if self._map[key] != 0)

    @property
    def all_dimensions(self) -> tuple[str, ...]:
        return tuple(key for key, _ in self._items)

    def get(self, dimension: str) -> Number:
        return self._map.get(dimension, 0)

    def as_dict(self) -> dict[str, Number]:
        return dict(self._items)

    def copy(self) -> "ResourceVector":
        return ResourceVector(self._items)

    def __getitem__(self, dimension: str) -> Number:
        return self.get(dimension)

    def __bool__(self) -> bool:
        return any(value > 0 for _, value in self._items)

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, ResourceVector):
            return self._items == other._items or self.as_dict() == other.as_dict()
        if isinstance(other, Mapping):
            return self == ResourceVector(other)
        return NotImplemented

    def __hash__(self) -> int:
        return self._hash

    def __repr__(self) -> str:
        body = ", ".join(f"{key}={value!r}" for key, value in self._items if value != 0)
        return f"ResourceVector({body})"

    def __add__(self, other: "ResourceVector | Mapping[str, Number]") -> "ResourceVector":
        other_vec = as_vector(other)
        data = self.as_dict()
        for key, value in other_vec._items:
            data[key] = data.get(key, 0) + value
        return ResourceVector(data)

    __radd__ = __add__

    def __sub__(self, other: "ResourceVector | Mapping[str, Number]") -> "ResourceVector":
        return self.subtract(other, strict=True)

    def subtract(
        self,
        other: "ResourceVector | Mapping[str, Number]",
        *,
        strict: bool = True,
    ) -> "ResourceVector":
        """Return self - other.

        With ``strict=False``, negative components are clamped to zero.  With
        strict true, subtraction that would make a component negative raises
        :class:`InsufficientResources`.
        """
        other_vec = as_vector(other)
        data = self.as_dict()
        for key, value in other_vec._items:
            new_value = data.get(key, 0) - value
            if new_value < 0 and strict:
                raise InsufficientResources(
                    f"cannot subtract {key}={value} from available {data.get(key, 0)}"
                )
            data[key] = max(0, new_value)
        return ResourceVector(data)

    def deficit(self, available: "ResourceVector | Mapping[str, Number]") -> "ResourceVector":
        """Return the positive per-dimension shortfall against ``available``."""
        avail = as_vector(available)
        data: dict[str, Number] = {}
        for key, value in self._map.items():
            short = value - avail.get(key)
            if short > 0:
                data[key] = short
        return ResourceVector(data)

    def fits_in(self, capacity: "ResourceVector | Mapping[str, Number]") -> bool:
        cap = as_vector(capacity)
        return all(value <= cap.get(key) for key, value in self._map.items())

    def covers(self, requirement: "ResourceVector | Mapping[str, Number]") -> bool:
        """True if this vector is at least as large as ``requirement``."""
        req = as_vector(requirement)
        return req.fits_in(self)

    def max_components(self, other: "ResourceVector | Mapping[str, Number]") -> "ResourceVector":
        other_vec = as_vector(other)
        data: dict[str, Number] = {}
        for dim in set(self._map) | set(other_vec._map):
            data[dim] = max(self.get(dim), other_vec.get(dim))
        return ResourceVector(data)

    def to_json(self) -> dict[str, Number]:
        return self.as_dict()


ZERO_VECTOR = ResourceVector()


class InsufficientResources(ValueError):
    """Raised when a strict resource subtraction would be negative."""


def as_vector(value: "ResourceVector | Mapping[str, Number] | None") -> ResourceVector:
    if value is None:
        return ZERO_VECTOR
    if isinstance(value, ResourceVector):
        return value
    return ResourceVector(value)
