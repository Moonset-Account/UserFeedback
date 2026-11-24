"""Error-budget allocation and two-layer reconciliation component."""

from .allocation import (
    allocate,
    allocate_with_pending,
    allocation_diff,
    diff_allocations,
    largest_remainder_allocation,
    normalize_amount,
    normalize_weights,
)
from .errors import (
    DuplicateRequestError,
    ErrorBudgetError,
    IncidentNotFoundError,
    InvalidAmountError,
    InvalidWeightsError,
    LedgerInvariantError,
    NoAllocatableWeightError,
    RequestNotFoundError,
    RevocationExceedsBalanceError,
    ValidationError,
)
from .ledger import (
    ErrorBudgetLedger,
    LedgerEntry,
    Reconciliation,
    settle_events,
)

__all__ = [
    "ErrorBudgetLedger",
    "LedgerEntry",
    "Reconciliation",
    "settle_events",
    "largest_remainder_allocation",
    "allocate",
    "allocate_with_pending",
    "diff_allocations",
    "allocation_diff",
    "normalize_amount",
    "normalize_weights",
    "ErrorBudgetError",
    "ValidationError",
    "InvalidAmountError",
    "InvalidWeightsError",
    "NoAllocatableWeightError",
    "RequestNotFoundError",
    "DuplicateRequestError",
    "IncidentNotFoundError",
    "RevocationExceedsBalanceError",
    "LedgerInvariantError",
]
