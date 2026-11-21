"""Input events and the module-mapping timeline.

The three event kinds carry a timestamp, a process id and — for samples —
a stack of absolute instruction addresses, root first, leaf last.

Ordering rules implemented here (see :func:`sort_events`):

* a smaller ``(timestamp, phase, sequence)`` sorts first;
* at the *same* timestamp, unloads are applied before loads, and loads
  before samples, so an address reused by a new module in the same
  instant is attributed to the module that is mapped *after* the change;
* events that share a timestamp *and* a phase keep their input order
  (``sequence``), which makes the sort stable and reproducible.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence as ABCSequence
from dataclasses import dataclass, field
from typing import List, Tuple

from .errors import ValidationError
from .model import require_int, require_text

__all__ = [
    "EVENT_LOAD",
    "EVENT_UNLOAD",
    "EVENT_SAMPLE",
    "PHASE_ORDER",
    "LoadEvent",
    "UnloadEvent",
    "SampleEvent",
    "sort_events",
]

EVENT_LOAD = "load"
EVENT_UNLOAD = "unload"
EVENT_SAMPLE = "sample"

#: Sort priority for events sharing one timestamp.
PHASE_ORDER = {EVENT_UNLOAD: 0, EVENT_LOAD: 1, EVENT_SAMPLE: 2}


@dataclass(frozen=True)
class LoadEvent:
    """Map ``module@version`` at ``base`` for process ``pid`` at ``timestamp``."""

    timestamp: int
    pid: int
    module: str
    version: str
    base: int
    size: int
    kind: str = field(default=EVENT_LOAD, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        require_int(self.timestamp, "load timestamp")
        require_int(self.pid, "load pid", minimum=0)
        require_text(self.module, "load module")
        require_text(self.version, "load version")
        require_int(self.base, "load base", minimum=0)
        require_int(self.size, "load size", minimum=1)
        if self.base + self.size > 1 << 64:
            raise ValidationError("load mapping leaves the 64-bit address space")

    @property
    def end(self) -> int:
        return self.base + self.size


@dataclass(frozen=True)
class UnloadEvent:
    """Unmap ``module@version`` (or whatever that module maps) for ``pid``."""

    timestamp: int
    pid: int
    module: str
    version: str | None = None
    base: int | None = None
    kind: str = field(default=EVENT_UNLOAD, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        require_int(self.timestamp, "unload timestamp")
        require_int(self.pid, "unload pid", minimum=0)
        require_text(self.module, "unload module")
        if self.version is not None:
            require_text(self.version, "unload version")
        if self.base is not None:
            require_int(self.base, "unload base", minimum=0)


@dataclass(frozen=True)
class SampleEvent:
    """A raw stack sample: ``frames`` are absolute addresses, root first."""

    timestamp: int
    pid: int
    frames: Tuple[int, ...]
    kind: str = field(default=EVENT_SAMPLE, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        require_int(self.timestamp, "sample timestamp")
        require_int(self.pid, "sample pid", minimum=0)
        frames = self.frames
        if isinstance(frames, (str, bytes)) or not isinstance(
            frames, ABCSequence
        ):
            raise ValidationError("sample frames must be a sequence of addresses")
        if not frames:
            raise ValidationError("a sample must contain at least one frame")
        checked = []
        for index, frame in enumerate(frames):
            checked.append(
                require_int(frame, f"sample frame {index}", minimum=0)
            )
        object.__setattr__(self, "frames", tuple(checked))


@dataclass(frozen=True)
class OrderedEvent:
    """An input event plus the tie-break keys used while replaying it."""

    timestamp: int
    phase: int
    sequence: int
    event: object

    @property
    def sort_key(self) -> Tuple[int, int, int]:
        return (self.timestamp, self.phase, self.sequence)


def sort_events(events: Iterable[object]) -> List[OrderedEvent]:
    """Return events in replay order, ignoring their input order.

    The sort key is ``(timestamp, phase, input position)``.  ``phase`` is
    ``unload < load < sample`` so same-instant unmaps are applied before
    maps, and both before samples that observe the new layout.
    """

    ordered: List[OrderedEvent] = []
    for sequence, event in enumerate(events):
        kind = getattr(event, "kind", None)
        if kind not in PHASE_ORDER:
            raise ValidationError(
                f"event #{sequence} has unknown kind {kind!r}; "
                f"expected one of {sorted(PHASE_ORDER)}"
            )
        ordered.append(
            OrderedEvent(
                timestamp=event.timestamp,
                phase=PHASE_ORDER[kind],
                sequence=sequence,
                event=event,
            )
        )
    ordered.sort(key=lambda item: item.sort_key)
    return ordered
