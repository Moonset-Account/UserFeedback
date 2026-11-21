"""Public API for the change-window scheduler module."""

from __future__ import annotations

from .model import (
    ChangePlan,
    FailurePlan,
    ForwardStep,
    Interval,
    RollbackAction,
    Step,
)
from .scheduler import SchedulerError, plan_change, verify_plan

__all__ = [
    "Interval",
    "Step",
    "ForwardStep",
    "RollbackAction",
    "FailurePlan",
    "ChangePlan",
    "SchedulerError",
    "plan_change",
    "verify_plan",
]

__version__ = "1.0.0"
