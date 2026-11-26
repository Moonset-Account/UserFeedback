"""Convenience import path for the offline transfer approval solution.

The implementation lives in the :mod:`transfer_approval` package.  This module
lets callers use both ``import solution`` and
``from transfer_approval import evaluate_case``.
"""

from transfer_approval.engine import (  # noqa: F401
    ApprovalInputError,
    OfflineTransferApproval,
    TranscriptValidationError,
    approve_transfer,
    assess_transfer,
    check_approval,
    check_transfer,
    evaluate,
    evaluate_case,
    evaluate_transfer,
    solve,
)
from transfer_approval.intervals import Interval  # noqa: F401

__all__ = [
    "ApprovalInputError",
    "OfflineTransferApproval",
    "TranscriptValidationError",
    "Interval",
    "approve_transfer",
    "evaluate_case",
    "evaluate_transfer",
    "evaluate",
    "solve",
    "assess_transfer",
    "check_approval",
    "check_transfer",
]
