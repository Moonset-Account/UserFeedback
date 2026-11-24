"""Window physical planning library.

Public API:

* specifications: :class:`WindowSpec`, :class:`OrderKey`, :class:`Direction`,
  :class:`Nulls`, :class:`TiePolicy`, :class:`WindowFunction`;
* planning: :func:`plan_windows`, :class:`WindowPlan`, :class:`PlanStep`,
  :class:`StepKind`, :class:`PhysKey`;
* execution: :func:`execute_plan`, :class:`ExecutionResult`;
* verification: :func:`verify`, :func:`reference_values`,
  :func:`reaudit_plan_fees`, :class:`VerificationReport`;
* data and fees: :func:`load_rows`, :class:`FeeSchedule`,
  :func:`default_fee_schedule`.
"""

from .cost import (
    DEFAULT_COMPARISON_BRACKETS,
    DEFAULT_LENGTH_FEE,
    FeeSchedule,
    MissingFeeError,
    default_fee_schedule,
)
from .executor import ExecutedStep, ExecutionResult, execute_plan
from .logical import materialise_logical
from .physical import (
    INITIAL_KEY,
    KeyRole,
    PhysKey,
    PhysKeyPart,
    can_reuse,
    candidate_keys,
    measured_partial_sort,
    measured_stable_sort,
    partial_sort_key,
    partition_prefix_match,
)
from .planner import PlanStep, StepKind, WindowPlan, plan_windows
from .spec import (
    MAX_WINDOWS,
    ROWNO_COLUMN,
    Direction,
    Nulls,
    OrderKey,
    TiePolicy,
    WindowFunction,
    WindowSpec,
)
from .validation import DependencyError, SpecError, load_rows, topo_order, validate_specs
from .verification import (
    VerificationError,
    VerificationReport,
    reaudit_plan_fees,
    reference_values,
    verify,
)

__all__ = [
    "DEFAULT_COMPARISON_BRACKETS",
    "DEFAULT_LENGTH_FEE",
    "DependencyError",
    "Direction",
    "ExecutedStep",
    "ExecutionResult",
    "FeeSchedule",
    "INITIAL_KEY",
    "KeyRole",
    "MAX_WINDOWS",
    "MissingFeeError",
    "Nulls",
    "OrderKey",
    "PhysKey",
    "PhysKeyPart",
    "PlanStep",
    "ROWNO_COLUMN",
    "SpecError",
    "StepKind",
    "TiePolicy",
    "VerificationError",
    "VerificationReport",
    "WindowFunction",
    "WindowPlan",
    "WindowSpec",
    "can_reuse",
    "candidate_keys",
    "default_fee_schedule",
    "execute_plan",
    "load_rows",
    "materialise_logical",
    "measured_partial_sort",
    "measured_stable_sort",
    "partial_sort_key",
    "partition_prefix_match",
    "plan_windows",
    "reaudit_plan_fees",
    "reference_values",
    "topo_order",
    "validate_specs",
    "verify",
]
