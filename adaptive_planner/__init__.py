"""Small exact adaptive join planner.

Public API:

* :func:`parse_problem`, :func:`load_problem`
* :func:`plan_strategy`
* :func:`verify_strategy`, :func:`audit_decision_traces`
"""

from .model import (
    Block,
    CostTable,
    Edge,
    PlannerError,
    Problem,
    RowCost,
    Scenario,
    TableSpec,
    load_problem,
    parse_problem,
)
from .planner import NoFeasiblePlanError, Optimizer, plan_strategy
from .validator import (
    ValidationReport,
    audit_decision_traces,
    validate_no_scenario_leak,
    verify_files,
    verify_strategy,
)

# Concise aliases for callers that prefer a verb-based API.
plan = plan_strategy
verify = verify_strategy

__all__ = [
    "Block",
    "CostTable",
    "Edge",
    "NoFeasiblePlanError",
    "Optimizer",
    "PlannerError",
    "Problem",
    "RowCost",
    "Scenario",
    "TableSpec",
    "ValidationReport",
    "audit_decision_traces",
    "load_problem",
    "parse_problem",
    "plan_strategy",
    "plan",
    "verify_strategy",
    "verify",
    "validate_no_scenario_leak",
    "verify_files",
]
