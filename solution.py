"""Compatibility facade for callers expecting a top-level ``solution`` module.

The implementation lives in the dependency-free :mod:`volume_audit` package.
"""

from volume_audit import (
    AuditDataError,
    HandoffAuditor,
    Interval,
    VolumeAuditError,
    audit,
    audit_document,
    audit_handoffs,
    calculate_coverage,
    coerce_interval,
    evaluate_handoffs,
    merge_intervals,
    subtract_covered,
    verify,
    verify_handoffs,
)

__all__ = [
    "AuditDataError",
    "HandoffAuditor",
    "Interval",
    "VolumeAuditError",
    "audit",
    "audit_document",
    "audit_handoffs",
    "calculate_coverage",
    "coerce_interval",
    "evaluate_handoffs",
    "merge_intervals",
    "subtract_covered",
    "verify",
    "verify_handoffs",
]
