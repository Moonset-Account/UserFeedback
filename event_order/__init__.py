"""Public API for the event-order adjudication module."""

from .core import (
    ALL_SCOPES,
    DEFAULT_SCOPE,
    OUTCOME_AFTER,
    OUTCOME_BEFORE,
    OUTCOME_CONFLICT,
    OUTCOME_SAME,
    OUTCOME_UNKNOWN,
    ChainStep,
    Claim,
    Conflict,
    DuplicateEvidenceError,
    EvidenceChain,
    EvidenceNotFoundError,
    EventOrderError,
    OrderAdjudicator,
    OrderVerdict,
    Retraction,
    SourceRemovalTest,
    ValidationError,
    adjudicate_order,
    build_adjudicator,
)

__version__ = "1.0.0"

# Readable aliases for callers that prefer a domain-specific class name.
EventOrderAdjudicator = OrderAdjudicator
Adjudicator = OrderAdjudicator

__all__ = [
    "ALL_SCOPES",
    "DEFAULT_SCOPE",
    "OUTCOME_AFTER",
    "OUTCOME_BEFORE",
    "OUTCOME_CONFLICT",
    "OUTCOME_SAME",
    "OUTCOME_UNKNOWN",
    "ChainStep",
    "Claim",
    "Conflict",
    "DuplicateEvidenceError",
    "EvidenceChain",
    "EvidenceNotFoundError",
    "EventOrderError",
    "OrderAdjudicator",
    "EventOrderAdjudicator",
    "Adjudicator",
    "OrderVerdict",
    "Retraction",
    "SourceRemovalTest",
    "ValidationError",
    "adjudicate_order",
    "build_adjudicator",
]
