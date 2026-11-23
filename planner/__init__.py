"""多机训练抢占规划器（标准库实现，Python 3.11）。

公开 API 见 ``planner.model`` 与 ``planner.search``。
"""

from planner.model import (
    CheckpointOption,
    ExistingJob,
    JobPlacement,
    MultiNodeDomainRequirement,
    NewJob,
    Node,
    SameMachineRequirement,
    validate_inputs,
)
from planner.diagnose import FailureConstraint
from planner.placement import PlacementResult
from planner.quota import ScheduledWrite, schedule_writes
from planner.search import PlanResult, plan

__all__ = [
    "CheckpointOption",
    "ExistingJob",
    "FailureConstraint",
    "JobPlacement",
    "MultiNodeDomainRequirement",
    "NewJob",
    "Node",
    "PlacementResult",
    "PlanResult",
    "SameMachineRequirement",
    "ScheduledWrite",
    "plan",
    "schedule_writes",
    "validate_inputs",
]
