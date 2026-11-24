"""Public API for the packet planning module."""

from .planner import (
    InfeasiblePlanError,
    InvalidInputError,
    MaterialPlan,
    PacketPlan,
    PacketPlanner,
    PaddingBudgetExceeded,
    PaddingLimit,
    PlanningError,
    RecordDoesNotFitError,
    SegmentPlan,
    plan_packets,
    try_plan_packets,
)

__version__ = "1.0.0"

__all__ = [
    "InfeasiblePlanError",
    "InvalidInputError",
    "MaterialPlan",
    "PacketPlan",
    "PacketPlanner",
    "PaddingBudgetExceeded",
    "PaddingLimit",
    "PlanningError",
    "RecordDoesNotFitError",
    "SegmentPlan",
    "plan_packets",
    "try_plan_packets",
]
