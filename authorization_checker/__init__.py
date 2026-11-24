"""Authorization reconciliation for shared service-account handover."""

from .engine import AuthorizationChecker, evaluate_authorization

# Intuitive functional alias for callers describing the task as "check auth".
check_authorization = evaluate_authorization
from .factory import (
    build_approval,
    build_request,
    interval_from_mapping,
    parse_temporal,
)
from .models import (
    Approval,
    ApprovalKind,
    AuthorizationRequest,
    Identity,
    Interval,
    NaturalPerson,
    Scope,
    date_interval,
    merge_intervals,
    normalize_kind,
    subtract_intervals,
)
from .registry import (
    IdentityConflictError,
    PersonRegistry,
    UnresolvedIdentityError,
)
from .results import (
    AuthorizationDecision,
    Finding,
    Grant,
    IndependenceReport,
    PersonApproval,
    RemediationOption,
    ResponsiblePair,
)

__version__ = "1.0.0"

__all__ = [
    "Approval",
    "ApprovalKind",
    "AuthorizationChecker",
    "AuthorizationDecision",
    "AuthorizationRequest",
    "Finding",
    "Grant",
    "Identity",
    "IdentityConflictError",
    "IndependenceReport",
    "Interval",
    "NaturalPerson",
    "PersonApproval",
    "PersonRegistry",
    "RemediationOption",
    "ResponsiblePair",
    "Scope",
    "UnresolvedIdentityError",
    "build_approval",
    "build_request",
    "check_authorization",
    "date_interval",
    "evaluate_authorization",
    "interval_from_mapping",
    "merge_intervals",
    "normalize_kind",
    "parse_temporal",
    "subtract_intervals",
]
