"""Offline key ceremony custodian scheduler.

The package implements the defensive scheduling checks described in the
task: availability, slot eligibility, one slot per person per round,
independence for dual-control keys, continuity/handover checks, exact
minimum-change dynamic programming, and first-unreachable-round diagnosis.
"""

from .api import (
    ScheduleResult,
    find_first_unreachable_round,
    minimum_change_schedule,
    schedule_ceremony,
    schedule_from_dict,
    schedule_from_json,
)
from .errors import CeremonyInputError
from .model import CeremonySpec, load_spec

__all__ = [
    "CeremonyInputError",
    "CeremonySpec",
    "ScheduleResult",
    "find_first_unreachable_round",
    "load_spec",
    "minimum_change_schedule",
    "schedule_ceremony",
    "schedule_from_dict",
    "schedule_from_json",
]

__version__ = "1.0.0"
