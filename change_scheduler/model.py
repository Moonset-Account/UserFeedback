"""Public data model for the change-window scheduler.

All times are integer minutes counted from the start of the day.  Every
interval is half-open: ``[start, end)``.  A step occupies its duration
*inside* its execution window, and a rollback action occupies its duration
*inside* its duty window.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

__all__ = [
    "Interval",
    "Step",
    "ForwardStep",
    "RollbackAction",
    "FailurePlan",
    "ChangePlan",
]


def _require_int(value: object, field: str) -> None:
    """Reject ``bool``/``float``/etc. so silently-wrong input fails loudly."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} must be an int, got {type(value).__name__}")


@dataclass(frozen=True)
class Interval:
    """A half-open integer-minute interval ``[start, end)``."""

    start: int
    end: int

    def __post_init__(self) -> None:
        _require_int(self.start, "interval.start")
        _require_int(self.end, "interval.end")
        if self.start < 0:
            raise ValueError("interval.start must be >= 0")
        if self.end <= self.start:
            raise ValueError(
                f"interval must be non-empty half-open, got [{self.start}, {self.end})"
            )

    @property
    def length(self) -> int:
        return self.end - self.start

    def contains_span(self, start: int, end: int) -> bool:
        """Return ``True`` when ``[start, end)`` lies inside this interval."""
        return self.start <= start and end <= self.end


@dataclass(frozen=True)
class Step:
    """One forward change step and the rollback action that undoes it.

    Attributes
    ----------
    duration:
        Non-interruptible forward execution length in minutes.
    window:
        Execution window; the whole forward execution must fit inside it.
    rollback_duration:
        Length of the rollback action in minutes.
    duty_window:
        Duty window; the whole rollback action must fit inside it.
    name:
        Optional human readable label, echoed back in the plan.
    """

    duration: int
    window: Interval
    rollback_duration: int
    duty_window: Interval
    name: str = ""

    def __post_init__(self) -> None:
        _require_int(self.duration, "step.duration")
        _require_int(self.rollback_duration, "step.rollback_duration")
        if self.duration < 0:
            raise ValueError("step.duration must be >= 0")
        if self.rollback_duration < 0:
            raise ValueError("step.rollback_duration must be >= 0")
        if not isinstance(self.window, Interval):
            raise TypeError("step.window must be an Interval")
        if not isinstance(self.duty_window, Interval):
            raise TypeError("step.duty_window must be an Interval")
        if not isinstance(self.name, str):
            raise TypeError("step.name must be a string")


@dataclass(frozen=True)
class ForwardStep:
    """A concrete placement of one forward step; ``[start, end)``."""

    step_index: int  # 1-based
    name: str
    start: int
    end: int


@dataclass(frozen=True)
class RollbackAction:
    """A concrete placement of one rollback action; ``[start, end)``."""

    step_index: int  # 1-based
    name: str
    start: int
    end: int


@dataclass(frozen=True)
class FailurePlan:
    """Rollback plan for a failure declared at the end of ``failed_step``."""

    failed_step: int  # 1-based
    failure_time: int
    rollbacks: Tuple[RollbackAction, ...]

    @property
    def completion_time(self) -> Optional[int]:
        return self.rollbacks[-1].end if self.rollbacks else self.failure_time


@dataclass(frozen=True)
class ChangePlan:
    """Result returned by :func:`change_scheduler.plan_change`.

    ``feasible`` is ``False`` for an infeasible change; in that case
    ``start_time`` is ``None``, ``forward``/``failures`` are empty, and
    ``reason`` explains the blocking constraint.
    """

    feasible: bool
    start_time: Optional[int]
    deadline: int
    forward: Tuple[ForwardStep, ...]
    failures: Tuple[FailurePlan, ...]
    reason: Optional[str] = None
