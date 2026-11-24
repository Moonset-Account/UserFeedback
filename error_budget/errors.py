"""Exception types for the error-budget allocation ledger."""

from __future__ import annotations


class ErrorBudgetError(Exception):
    """Base class for all component errors."""


class ValidationError(ErrorBudgetError, ValueError):
    """Raised when caller-supplied facts or weights are invalid."""


class InvalidAmountError(ValidationError):
    """Raised when a charge amount is not a non-negative integral unit count."""


class InvalidWeightsError(ValidationError):
    """Raised when weights cannot define a proportional allocation."""


class NoAllocatableWeightError(InvalidWeightsError):
    """Raised when an allocation pool has no positive weight to distribute by."""


class RequestNotFoundError(ErrorBudgetError, KeyError):
    """Raised when an operation references an unknown request."""


class DuplicateRequestError(ErrorBudgetError):
    """Raised when a request ID is registered more than once."""


class IncidentNotFoundError(ErrorBudgetError, KeyError):
    """Raised when a revocation references an unknown incident."""


class RevocationExceedsBalanceError(ErrorBudgetError):
    """Raised when a revocation asks for more units than an incident holds."""


class LedgerInvariantError(ErrorBudgetError):
    """Raised if the service and distribution layers cease to reconcile."""
