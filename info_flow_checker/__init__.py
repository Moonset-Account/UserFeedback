"""Small exact information-flow checker for acyclic Boolean circuits.

The public API is intentionally small:

* :class:`CircuitBuilder`, :func:`circuit_from_dict`, :func:`circuit_to_dict`
* :class:`Policy`
* :func:`check_information_flow`
* :func:`evaluate`, :func:`evaluate_nodes`
* :func:`propagate_constants`
"""

from __future__ import annotations

from .checker import (
    BoundPolicy,
    Counterexample,
    FirstDifference,
    ReplayOutcome,
    SecurityReport,
    check_information_flow,
    replay_counterexample,
)
from .circuit import (
    AND,
    CONST,
    INPUT,
    MUX,
    NOT,
    OR,
    Circuit,
    CircuitBuilder,
    Node,
    Output,
    circuit_from_dict,
    circuit_to_dict,
)
from .errors import (
    CircuitEvaluationError,
    CircuitValidationError,
    InfoFlowError,
    LimitExceededError,
    PolicyValidationError,
)
from .optimize import propagate_constants
from .policy import MAX_PUBLIC_BITS, MAX_SECRET_BITS, Policy
from .simulator import evaluate, evaluate_nodes

__all__ = [
    "AND",
    "OR",
    "NOT",
    "MUX",
    "CONST",
    "INPUT",
    "BoundPolicy",
    "Circuit",
    "CircuitBuilder",
    "CircuitEvaluationError",
    "CircuitValidationError",
    "Counterexample",
    "FirstDifference",
    "InfoFlowError",
    "LimitExceededError",
    "MAX_PUBLIC_BITS",
    "MAX_SECRET_BITS",
    "Node",
    "Output",
    "Policy",
    "PolicyValidationError",
    "ReplayOutcome",
    "SecurityReport",
    "check_information_flow",
    "circuit_from_dict",
    "circuit_to_dict",
    "evaluate",
    "evaluate_nodes",
    "propagate_constants",
    "replay_counterexample",
]

__version__ = "1.0.0"
