"""Controllable clocks.

The planner and the connector never read the wall clock directly. A clock is
any object with a parameterless ``now()`` returning monotonic-ish seconds as a
float. :class:`FakeClock` is manually advanced which makes snapshot expiry
fully deterministic in tests.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    def now(self) -> float:
        """Return the current time in seconds."""
        ...


class SystemClock:
    """Clock backed by :func:`time.monotonic` (safe: not network related)."""

    def now(self) -> float:
        import time

        return time.monotonic()


class FakeClock:
    """Manually controlled clock for tests."""

    def __init__(self, start: float = 1000.0) -> None:
        self._now = float(start)

    def now(self) -> float:
        return self._now

    def advance(self, seconds: float) -> float:
        if seconds < 0:
            raise ValueError("cannot move the clock backwards")
        self._now += float(seconds)
        return self._now
