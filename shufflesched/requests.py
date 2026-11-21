"""Typed request objects used when driving the scheduler.

Attempts and loss notifications are identified by opaque string ids supplied by
the caller.  Every mutation of the ``executor`` argument accepts a plain string
or an object exposing ``.executor_id`` (see :func:`executor_id_of`), which keeps
the scheduler usable both from a test harness and from a real driver.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping


def executor_id_of(executor: Any) -> str:
    """Best-effort extraction of a stable executor identifier."""
    if isinstance(executor, str):
        return executor
    ident = getattr(executor, "executor_id", None)
    if ident is not None:
        return str(ident)
    ident = getattr(executor, "id", None)
    if ident is not None:
        return str(ident)
    raise TypeError(f"cannot derive an executor id from {executor!r}")


@dataclass(frozen=True)
class AttemptSpec:
    """A request to launch one speculative attempt for ``partition``."""

    attempt_id: str
    partition: str
    executor: str


@dataclass(frozen=True)
class LossEvent:
    """A notification that ``executor`` was lost at scheduler time ``ts``."""

    event_id: str
    executor: str
    ts: int = 0


@dataclass(frozen=True)
class CompletionSpec:
    """A (possibly late) success callback for ``attempt_id``.

    ``reads`` maps upstream partition id -> BlockId that the attempt actually
    read.  When omitted the attempt's launch-time snapshot is used.  For a
    source attempt ``reads`` must be empty.
    """

    attempt_id: str
    executor: str
    reads: Mapping[str, Any] | None = None
    payload: Any = None


@dataclass(frozen=True)
class SourceWrite:
    """A concrete Shuffle block handed to the scheduler for a source partition."""

    attempt_id: str
    executor: str
    payload: Any = None


class ReadSet:
    """Normalised accessor over an upstream read mapping."""

    __slots__ = ("_items",)

    def __init__(self, reads: Mapping[str, Any] | Iterable[Any] | None) -> None:
        if reads is None:
            items: dict[str, Any] = {}
        elif isinstance(reads, Mapping):
            items = dict(reads)
        else:
            items = {}
            for entry in reads:
                if isinstance(entry, Mapping):
                    pid, value = next(iter(entry.items()))
                elif isinstance(entry, (tuple, list)) and len(entry) == 2:
                    pid, value = entry
                else:
                    raise TypeError(f"cannot interpret read entry {entry!r}")
                items[str(pid)] = value
        self._items = items

    def __len__(self) -> int:
        return len(self._items)

    def keys(self):
        return self._items.keys()

    def items(self):
        return self._items.items()

    def get(self, pid: str) -> Any:
        return self._items.get(pid)

    def as_dict(self) -> dict[str, Any]:
        return dict(self._items)


__all__ = [
    "executor_id_of",
    "AttemptSpec",
    "CompletionSpec",
    "LossEvent",
    "SourceWrite",
    "ReadSet",
]
