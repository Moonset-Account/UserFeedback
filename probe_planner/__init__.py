"""Budget-aware synthetic probe placement and fault-pair discrimination."""

from .api import (
    ProbePlacementPlanner,
    build_probes,
    evaluate_selection,
    plan_placement,
    plan_probes,
    solve,
)
from .errors import InfeasibleCoverageError, ProbePlannerError, ValidationError
from .model import FAILED, MISSING, OK, Fault, Probe
from .result import PlacementResult
from .semantics import probe_outcome

# Friendly aliases for callers that use the problem's wording.
choose_probes = plan_placement
place_probes = plan_placement
select_probe_placement = plan_placement
select_probes = plan_probes
ProbePlanner = ProbePlacementPlanner
plan = plan_placement

__all__ = [
    "FAILED",
    "MISSING",
    "OK",
    "Fault",
    "InfeasibleCoverageError",
    "PlacementResult",
    "Probe",
    "ProbePlacementPlanner",
    "ProbePlanner",
    "ProbePlannerError",
    "ValidationError",
    "build_probes",
    "choose_probes",
    "evaluate_selection",
    "plan",
    "plan_placement",
    "plan_probes",
    "place_probes",
    "probe_outcome",
    "select_probes",
    "select_probe_placement",
    "solve",
]

__version__ = "1.0.0"
