"""Optimal offline replacement planning for a small block cache."""

from .baseline import access_distance_greedy_plan
from .errors import (
    BlockCacheError,
    InfeasiblePlanError,
    InvalidInputError,
    InvalidRequestError,
    PlanVerificationError,
)
from .interpreter import TickInterpreter, VerificationReport, verify_plan
from .model import (
    ActiveWritebackCheckpoint,
    Checkpoint,
    InitialResident,
    Page,
    Pin,
    Read,
    Request,
    ResidentCheckpoint,
    Unpin,
    Write,
    coerce_requests,
)
from .plan import (
    AdvanceEvent,
    BeginRequestEvent,
    EvictEvent,
    GenerationRecord,
    OccupancySample,
    Plan,
    ResourceInterval,
    StartWritebackEvent,
    WritebackAction,
)
from .planner import CachePlanner

__all__ = [
    "CachePlanner",
    "TickInterpreter",
    "VerificationReport",
    "verify_plan",
    "access_distance_greedy_plan",
    "Page",
    "Read",
    "Write",
    "Pin",
    "Unpin",
    "Request",
    "coerce_requests",
    "InitialResident",
    "Checkpoint",
    "ResidentCheckpoint",
    "ActiveWritebackCheckpoint",
    "Plan",
    "WritebackAction",
    "GenerationRecord",
    "ResourceInterval",
    "OccupancySample",
    "BeginRequestEvent",
    "EvictEvent",
    "StartWritebackEvent",
    "AdvanceEvent",
    "BlockCacheError",
    "InvalidInputError",
    "InvalidRequestError",
    "InfeasiblePlanError",
    "PlanVerificationError",
]
