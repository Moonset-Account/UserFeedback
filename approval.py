"""Compatibility module exposing the public approval API."""

from main import (  # noqa: F401
    ApprovalInputError,
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

__all__ = [
    "ApprovalInputError",
    "TranscriptValidationError",
    "approve_transfer",
    "assess_transfer",
    "check_approval",
    "check_transfer",
    "evaluate",
    "evaluate_case",
    "evaluate_transfer",
    "solve",
]
