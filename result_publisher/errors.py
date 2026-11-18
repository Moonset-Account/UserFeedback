"""Exceptions raised by the result publishing module."""

from __future__ import annotations


class PublisherError(Exception):
    """Base class for all errors raised by this package."""


class QuotaExceededError(PublisherError):
    """Raised when a dataset/purpose scope does not have enough quota.

    The caller never observes a cached result or any partially generated
    value for the request; the reservation is rolled back before this
    exception leaves the publisher.
    """

    def __init__(self, scope: str, requested: float, available: float) -> None:
        self.scope = scope
        self.requested = requested
        self.available = available
        super().__init__(
            f"quota exceeded for scope {scope!r}: requested {requested}, "
            f"available {available}"
        )


class UnknownScopeError(PublisherError):
    """Raised when no budget has been granted for a dataset/purpose pair."""

    def __init__(self, scope: str) -> None:
        self.scope = scope
        super().__init__(f"no budget registered for scope {scope!r}")


class PublicationError(PublisherError):
    """Raised when the local noise adapter fails to produce a result.

    Any quota reserved for the failed publication has already been released
    by the time this exception is raised. The original adapter exception is
    available as ``__cause__`` (via ``raise ... from exc``).
    """
