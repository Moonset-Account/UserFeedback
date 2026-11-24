"""Errors for the synthetic-check probe placement component."""

from __future__ import annotations

from typing import Any


class ProbePlannerError(ValueError):
    """Base class for invalid planner input or impossible placement."""


class ValidationError(ProbePlannerError):
    """Raised when candidate probes, faults, or parameters are malformed."""


class InfeasibleCoverageError(ProbePlannerError):
    """Raised when required entries cannot be covered within the budget."""

    def __init__(
        self,
        message: str,
        *,
        required_entries: frozenset[Any] | None = None,
        missing_entries: frozenset[Any] | None = None,
        minimum_cost: Any = None,
        budget: Any = None,
    ) -> None:
        super().__init__(message)
        self.required_entries = required_entries
        self.missing_entries = missing_entries
        self.minimum_cost = minimum_cost
        self.budget = budget
