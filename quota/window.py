"""Rolling-window bucket primitives."""

from __future__ import annotations

from collections import deque
from typing import Iterator


class RollingWindow:
    """Timestamps of admitted requests inside a fixed-width rolling window.

    A request admitted at time ``t`` occupies the half-open interval
    ``[t, t + window_seconds)``; it stops counting exactly at ``t +
    window_seconds``. That makes the earliest retry instant exact and easy to
    assert on.
    """

    __slots__ = ("_events", "window_seconds")

    def __init__(self, window_seconds: float) -> None:
        self.window_seconds = window_seconds
        self._events: deque[float] = deque()

    def prune(self, now: float) -> None:
        """Drop events that have fully left the window at ``now``."""

        cutoff = now - self.window_seconds
        events = self._events
        while events and events[0] <= cutoff:
            events.popleft()

    def count(self) -> int:
        return len(self._events)

    def oldest(self) -> float | None:
        return self._events[0] if self._events else None

    def record(self, now: float) -> None:
        """Record an event; callers must keep timestamps non-decreasing."""

        if self._events and now < self._events[-1]:
            raise AssertionError("window timestamps must be non-decreasing")
        self._events.append(now)

    def expire_at(self, now: float) -> float | None:
        """Earliest instant at which one more admission fits again."""

        oldest = self.oldest()
        if oldest is None:
            return now
        return oldest + self.window_seconds

    def __len__(self) -> int:
        return len(self._events)

    def events(self) -> Iterator[float]:
        return iter(tuple(self._events))
