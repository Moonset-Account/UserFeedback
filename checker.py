"""Compatibility module name for callers expecting ``checker``.

The implementation and documented API live in :mod:`static_check`.
"""

from static_check import *  # noqa: F401,F403
from static_check import (  # noqa: F401
    AuditReport,
    DisclosurePolicy,
    audit_flow,
    check,
    check_flow,
    reverse_identity_filter,
    reverse_scan,
)

__all__ = [
    "audit_flow",
    "check",
    "check_flow",
    "DisclosurePolicy",
    "AuditReport",
    "reverse_scan",
    "reverse_identity_filter",
]
