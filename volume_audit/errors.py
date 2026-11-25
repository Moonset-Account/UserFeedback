"""Exception types for the volume audit component."""

from __future__ import annotations


class VolumeAuditError(Exception):
    """Base class for errors raised by this package."""


class AuditDataError(VolumeAuditError):
    """Raised in strict mode when supplied audit data cannot be interpreted.

    ``issues`` contains structured validation findings when available.
    """

    def __init__(self, message: str, issues: list[dict] | None = None):
        super().__init__(message)
        self.issues = issues or []
