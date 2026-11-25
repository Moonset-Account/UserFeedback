"""Errors raised by the information-flow checker."""

from __future__ import annotations


class InfoFlowError(ValueError):
    """Base class for all domain-specific errors."""


class CircuitValidationError(InfoFlowError):
    """Raised when a circuit definition is malformed or cyclic."""


class CircuitEvaluationError(InfoFlowError):
    """Raised when an assignment cannot be evaluated by a circuit."""


class PolicyValidationError(InfoFlowError):
    """Raised when labels or disclosure policy are invalid."""


class LimitExceededError(PolicyValidationError):
    """Raised when the public/secret input bound is exceeded."""
