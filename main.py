"""Project entry point.

Use ``python3 main.py examples/synthetic_case.json`` or import
:func:`evaluate_case` from this module.
"""

from transfer_approval.cli import main
from transfer_approval.engine import (
    ApprovalInputError,
    TranscriptValidationError,
    approve_transfer,
    assess_transfer,
    check_transfer,
    evaluate_case,
    evaluate_transfer,
)

solve = evaluate_case
evaluate = evaluate_case
check_approval = approve_transfer

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
    "main",
    "solve",
]


if __name__ == "__main__":
    raise SystemExit(main())
