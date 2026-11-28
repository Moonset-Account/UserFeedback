"""Exceptions used by the fpiso isolation model."""

from __future__ import annotations


class FpisoError(Exception):
    """Base class for all model-specific errors."""


class FormatError(FpisoError, ValueError):
    """An invalid floating-point format or configuration was requested."""


class OperandError(FpisoError, TypeError):
    """An operand was not an exact rational value."""


class IsolationError(FpisoError, RuntimeError):
    """An operation tried to cross a protection-domain boundary."""


class NoActiveDomainError(IsolationError):
    """An operation requiring an active protection domain was attempted."""


class DomainOwnershipError(IsolationError):
    """A domain was used with a runtime that does not own it."""


class DomainLifecycleError(IsolationError):
    """A domain was entered or exited in an invalid order."""


class SuspendedDomainError(IsolationError):
    """A suspended (paused) domain's state was read or changed."""


class DomainActiveError(IsolationError):
    """A domain that is currently active (or on the stack) was re-entered."""
