"""Adjustable clock used by the LACP controller for deterministic testing."""

from __future__ import annotations


class ManualClock:
    """A monotonically adjustable virtual clock.

    The controller never reads the wall clock directly; every deadline is
    evaluated against this clock, which can only move forward.
    """

    def __init__(self, start: float = 0.0) -> None:
        self._now = float(start)

    def now(self) -> float:
        """Return the current virtual time."""
        return self._now

    def advance(self, seconds: float) -> float:
        """Move the clock forward by ``seconds`` (must be non-negative)."""
        if seconds < 0:
            raise ValueError("clock can only move forward")
        self._now += float(seconds)
        return self._now

    # Convenience alias matching common test-clock terminology.
    tick = advance
