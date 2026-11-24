"""Exceptions raised by the forensic allocator."""

from __future__ import annotations


class AllocationError(ValueError):
    """Base class for invalid allocation inputs or requests."""


class ValidationError(AllocationError):
    """Raised when exhibits or reviewers fail input validation."""


class UnknownReviewerError(AllocationError):
    """Raised when a portal view is requested for an unknown reviewer."""
