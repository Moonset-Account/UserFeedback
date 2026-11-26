"""Errors raised by the safe dump planner."""

from __future__ import annotations


class DumpPlannerError(ValueError):
    """Base class for invalid planner input or an unsafe result."""


class ValidationError(DumpPlannerError):
    """Raised when request data is structurally invalid in strict mode."""


class UnsafeOutputError(DumpPlannerError):
    """Raised if materialized output still contains a restricted address."""
