"""``sli_adjudicator`` — windowed SLI adjudication for release gating.

Public API (see ``README.md`` for the contract)::

    from sli_adjudicator import (
        Adjudicator, Rule, Observation, Correction,
        evaluate, compute_bounds, to_fraction,
        Counts, Decision, LogEntry, Bounds,
    )
"""

from .engine import Adjudicator, Bounds, compute_bounds, evaluate
from .model import (
    KIND_CORRECTION,
    KIND_OBSERVATION,
    OUTCOME_FAILURE,
    OUTCOME_PENDING,
    OUTCOME_SUCCESS,
    STATUS_FAILURE,
    STATUS_SUCCESS,
    STREAM_DOWNSTREAM,
    STREAM_EDGE,
    STREAM_RECEIPT,
    VERDICT_ALLOW,
    VERDICT_REJECT,
    VERDICT_WAIT,
    Correction,
    Counts,
    Decision,
    LogEntry,
    Observation,
    Rule,
    to_fraction,
)

__all__ = [
    "Adjudicator",
    "Bounds",
    "compute_bounds",
    "evaluate",
    "to_fraction",
    "Rule",
    "Observation",
    "Correction",
    "Counts",
    "Decision",
    "LogEntry",
    "KIND_OBSERVATION",
    "KIND_CORRECTION",
    "OUTCOME_SUCCESS",
    "OUTCOME_FAILURE",
    "OUTCOME_PENDING",
    "STATUS_SUCCESS",
    "STATUS_FAILURE",
    "STREAM_EDGE",
    "STREAM_DOWNSTREAM",
    "STREAM_RECEIPT",
    "VERDICT_ALLOW",
    "VERDICT_REJECT",
    "VERDICT_WAIT",
]

__version__ = "1.0.0"
