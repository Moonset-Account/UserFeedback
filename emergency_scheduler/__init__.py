"""Emergency repair scheduler.

Public API:
    Problem, Component, Stage, Service, ScheduleResult, Segment, Failure,
    ValidationError, solve
"""

from .brute_force import StateLimitReached, enumerate_best, first_failure
from .diagnose import greedy_diagnose
from .model import (
    Component,
    Failure,
    Problem,
    ScheduleResult,
    Segment,
    Service,
    Stage,
)
from .solver import solve
from .timeline import SimulationReport, Violation, simulate
from .validation import ValidationError

__all__ = [
    "Component",
    "Failure",
    "Problem",
    "ScheduleResult",
    "Segment",
    "Service",
    "Stage",
    "StateLimitReached",
    "ValidationError",
    "enumerate_best",
    "first_failure",
    "greedy_diagnose",
    "simulate",
    "solve",
    "SimulationReport",
    "Violation",
]
