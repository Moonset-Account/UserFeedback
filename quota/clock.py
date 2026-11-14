"""Injectable clocks used by the adjudicator and by the demonstrations."""

from __future__ import annotations

import threading
import time

from .config import require_finite_number


class SystemClock:
    """Monotonic wall clock for production embedding."""

    def now(self) -> float:
        return time.monotonic()


class ManualClock:
    """Deterministic clock for tests and demonstrations (thread-safe)."""

    def __init__(self, start: float = 0.0) -> None:
        self._time = require_finite_number(start, "clock start", minimum=0.0, exclusive=False)
        self._lock = threading.Lock()

    def now(self) -> float:
        with self._lock:
            return self._time

    def set(self, value: float) -> None:
        value = require_finite_number(value, "clock value", minimum=0.0, exclusive=False)
        with self._lock:
            self._time = value

    def advance(self, delta: float) -> float:
        delta = require_finite_number(delta, "clock delta", minimum=0.0, exclusive=False)
        with self._lock:
            self._time += delta
            return self._time
