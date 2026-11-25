"""Convenience top-level API for the logout-completion decider.

The implementation lives in the :mod:`identity_logout` package.  This module
provides a short import path and a few descriptive aliases.
"""

from __future__ import annotations

from identity_logout import (
    BarrierConfigurationError,
    CompletionNotReady,
    CompletionReceipt,
    Event,
    EventConflictError,
    EventKind,
    InvalidEventError,
    LogoutDecision,
    LogoutDecisionError,
    LogoutDecider,
    MissingEvidence,
    RevocationBarrier,
    Watermark,
    decide_logout_completion,
)

LogoutCompletionDecider = LogoutDecider
SessionLogoutDecider = LogoutDecider
evaluate_logout_completion = decide_logout_completion
can_issue_completion_receipt = decide_logout_completion

__all__ = [
    "LogoutDecider",
    "LogoutCompletionDecider",
    "SessionLogoutDecider",
    "decide_logout_completion",
    "evaluate_logout_completion",
    "can_issue_completion_receipt",
    "CompletionNotReady",
    "LogoutDecisionError",
    "InvalidEventError",
    "EventConflictError",
    "BarrierConfigurationError",
    "Event",
    "EventKind",
    "Watermark",
    "RevocationBarrier",
    "MissingEvidence",
    "CompletionReceipt",
    "LogoutDecision",
]
