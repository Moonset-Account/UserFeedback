"""Volume encryption handoff audit package.

The public, dependency-free API is intentionally small:

* :func:`audit_handoffs` -- audit a batch of devices.
* :func:`audit_document` -- audit one parsed JSON document.
* :func:`verify_handoffs` -- descriptive alias of :func:`audit_handoffs`.
* :func:`calculate_coverage` -- pure interval-coverage helper.
"""

from .auditor import (
    HandoffAuditor,
    audit,
    audit_document,
    audit_handoffs,
    evaluate_handoffs,
    verify,
    verify_handoffs,
)
from .errors import AuditDataError, VolumeAuditError
from .intervals import (
    Interval,
    calculate_coverage,
    coerce_interval,
    merge_intervals,
    subtract_covered,
)

__version__ = "1.0.0"

__all__ = [
    "AuditDataError",
    "HandoffAuditor",
    "Interval",
    "VolumeAuditError",
    "audit_document",
    "audit_handoffs",
    "evaluate_handoffs",
    "calculate_coverage",
    "coerce_interval",
    "merge_intervals",
    "subtract_covered",
    "verify",
    "verify_handoffs",
]
