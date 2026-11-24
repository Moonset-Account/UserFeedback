"""Public domain objects for the optimal block-cache planner."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .errors import InvalidInputError

_UNSET = object()


def _non_negative_int(value: Any, name: str, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidInputError(f"{name} must be an integer")
    if positive and value <= 0:
        raise InvalidInputError(f"{name} must be positive")
    if not positive and value < 0:
        raise InvalidInputError(f"{name} must be non-negative")
    return value


@dataclass(frozen=True)
class Page:
    """A uniquely identified page and its cache footprint in bytes."""

    id: str
    size: int
    writeback_ticks: int | None = None
    initial_value: Any = _UNSET

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id:
            raise InvalidInputError("page id must be a non-empty string")
        _non_negative_int(self.size, f"page {self.id!r} size", positive=True)
        if self.writeback_ticks is not None:
            _non_negative_int(
                self.writeback_ticks,
                f"page {self.id!r} writeback_ticks",
                positive=True,
            )


@dataclass(frozen=True)
class Read:
    page: str
    miss_ticks: int | None = None
    hit_ticks: int | None = None


@dataclass(frozen=True)
class Write:
    page: str
    value: Any = None
    ticks: int | None = None
    value_provided: bool = field(default=True, compare=False)

    def __init__(
        self,
        page: str,
        value: Any = _UNSET,
        ticks: int | None = None,
    ) -> None:
        # A small custom __init__ makes omitted values distinct from None while
        # retaining a frozen dataclass-like public construction.
        object.__setattr__(self, "page", page)
        object.__setattr__(self, "value_provided", value is not _UNSET)
        object.__setattr__(self, "value", None if value is _UNSET else value)
        object.__setattr__(self, "ticks", ticks)


@dataclass(frozen=True)
class Pin:
    page: str


@dataclass(frozen=True)
class Unpin:
    page: str


Request = Read | Write | Pin | Unpin


def coerce_requests(raw: Sequence[Request | tuple[Any, ...] | Mapping[str, Any]]) -> tuple[Request, ...]:
    """Accept both typed requests and compact tuple/dict representations."""

    out: list[Request] = []
    for i, item in enumerate(raw):
        if isinstance(item, (Read, Write, Pin, Unpin)):
            out.append(item)
            continue
        if isinstance(item, Mapping):
            kind = str(item.get("op", item.get("type", ""))).lower()
            page = item["page"]
            if kind in {"r", "read"}:
                out.append(Read(page, item.get("miss_ticks"), item.get("hit_ticks")))
            elif kind in {"w", "write"}:
                if "value" in item:
                    out.append(Write(page, item["value"], item.get("ticks")))
                else:
                    out.append(Write(page, ticks=item.get("ticks")))
            elif kind in {"p", "pin"}:
                out.append(Pin(page))
            elif kind in {"u", "unpin"}:
                out.append(Unpin(page))
            else:
                raise InvalidInputError(f"request {i} has unknown op {kind!r}")
            continue
        if isinstance(item, tuple) and item:
            kind = str(item[0]).upper()
            if kind == "R" and len(item) in {2, 3}:
                out.append(Read(item[1], None if len(item) == 2 else item[2]))
            elif kind == "W" and 2 <= len(item) <= 3:
                out.append(Write(item[1], item[2] if len(item) == 3 else _UNSET))
            elif kind == "P" and len(item) == 2:
                out.append(Pin(item[1]))
            elif kind == "U" and len(item) == 2:
                out.append(Unpin(item[1]))
            else:
                raise InvalidInputError(f"invalid tuple request at index {i}: {item!r}")
            continue
        raise InvalidInputError(f"request {i} is not recognized: {item!r}")
    return tuple(out)


@dataclass(frozen=True)
class InitialResident:
    """Initial cache and persistence state for one page."""

    generation: int = 0
    stable_generation: int | None = None
    dirty: bool = False
    value: Any = _UNSET
    persistent_value: Any = _UNSET
    pins: int = 0
    ready: bool = True

    def __post_init__(self) -> None:
        _non_negative_int(self.generation, "initial generation")
        _non_negative_int(self.pins, "initial pin count")
        if self.stable_generation is None:
            stable = self.generation if not self.dirty else max(0, self.generation - 1)
            object.__setattr__(self, "stable_generation", stable)
        else:
            _non_negative_int(self.stable_generation, "stable_generation")
        if self.dirty and self.generation <= self.stable_generation:  # type: ignore[operator]
            raise InvalidInputError("dirty initial page must have a newer generation than stable_generation")
        if not self.dirty and self.generation != self.stable_generation:
            raise InvalidInputError("clean initial page must have generation == stable_generation")


@dataclass(frozen=True)
class ActiveWritebackCheckpoint:
    page: str
    generation: int
    remaining_ticks: int
    duration_ticks: int
    byte_count: int
    value: Any = None
    job_id: int = 1
    started_at_tick: int | None = None

    def __post_init__(self) -> None:
        _non_negative_int(self.remaining_ticks, "active writeback remaining_ticks", positive=True)
        _non_negative_int(self.duration_ticks, "active writeback duration_ticks", positive=True)
        if self.remaining_ticks > self.duration_ticks:
            raise InvalidInputError("remaining_ticks cannot exceed duration_ticks")
        _non_negative_int(self.byte_count, "byte_count", positive=True)


@dataclass(frozen=True)
class ForegroundCheckpoint:
    request_index: int
    kind: str
    page: str | None
    remaining_ticks: int
    total_ticks: int

    def __post_init__(self) -> None:
        _non_negative_int(self.request_index, "request_index")
        _non_negative_int(self.remaining_ticks, "foreground remaining_ticks", positive=True)
        _non_negative_int(self.total_ticks, "foreground total_ticks", positive=True)
        if self.remaining_ticks > self.total_ticks:
            raise InvalidInputError("foreground remaining_ticks cannot exceed total_ticks")


@dataclass(frozen=True)
class Checkpoint:
    """A complete tick boundary from which suffix planning can resume."""

    at_tick: int
    cursor: int
    residents: Mapping[str, "ResidentCheckpoint"]
    generation: Mapping[str, int]
    stable_generation: Mapping[str, int]
    persistent_values: Mapping[str, Any]
    pins: Mapping[str, int] = field(default_factory=dict)
    active_writeback: ActiveWritebackCheckpoint | None = None
    foreground: ForegroundCheckpoint | None = None
    bytes_already_written: int = 0
    next_job_id: int = 1

    def __post_init__(self) -> None:
        _non_negative_int(self.at_tick, "checkpoint tick")
        _non_negative_int(self.cursor, "checkpoint cursor")
        _non_negative_int(self.bytes_already_written, "bytes_already_written")


@dataclass(frozen=True)
class ResidentCheckpoint:
    generation: int
    dirty: bool
    ready: bool = True
    value: Any = None
