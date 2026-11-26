"""Hybrid stream/batch execution scheduler.

The package provides:

* resource vectors and validated task/phase models;
* a pure legal-boundary release/recovery planner with wait-for cycle reports;
* a stateful scheduler with loans and plan application;
* a deterministic discrete-event runner for simultaneous reclamation inputs.
"""

from .events import Event, EventResult, run_event_log
from .model import (
    ClusterState,
    EventKind,
    Loan,
    Phase,
    TaskKind,
    TaskRuntime,
    TaskSpec,
    TaskStatus,
    topological_phase_ids,
)
from .planner import (
    ActionGroup,
    DeadlineViolation,
    PlannerConfig,
    PlanStep,
    ReclamationPlan,
    ReleaseAction,
    ReleaseOrderResult,
    ResourceCycle,
    ResumeCandidate,
    ResumeOrderResult,
    ReturnGrant,
    build_wait_edges,
    find_release_order,
    make_resource_cycle,
    plan_reclamation,
    plan_resume_order,
)
from .resources import InsufficientResources, ResourceVector, as_vector
from .scheduler import (
    HybridScheduler,
    ReclaimRequest,
    legal_boundary_candidates,
    next_legal_boundary,
)

__all__ = [
    "ActionGroup",
    "ClusterState",
    "DeadlineViolation",
    "Event",
    "EventKind",
    "EventResult",
    "HybridScheduler",
    "InsufficientResources",
    "Loan",
    "Phase",
    "PlanStep",
    "PlannerConfig",
    "ReclaimRequest",
    "ReclamationPlan",
    "ReleaseAction",
    "ReleaseOrderResult",
    "ResourceCycle",
    "ResourceVector",
    "ResumeCandidate",
    "ResumeOrderResult",
    "ReturnGrant",
    "TaskKind",
    "TaskRuntime",
    "TaskSpec",
    "TaskStatus",
    "as_vector",
    "build_wait_edges",
    "find_release_order",
    "legal_boundary_candidates",
    "make_resource_cycle",
    "next_legal_boundary",
    "plan_reclamation",
    "plan_resume_order",
    "run_event_log",
    "topological_phase_ids",
]

__version__ = "1.0.0"
