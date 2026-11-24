"""Exception types raised by :mod:`forensic_segments`."""

from __future__ import annotations


class ForensicSegmentsError(Exception):
    """Base class for every library error."""


class ValidationError(ForensicSegmentsError, ValueError):
    """Raised when caller-supplied evidence metadata is malformed."""


class UnknownSourceError(ForensicSegmentsError, KeyError):
    """Raised when an image or acquisition id cannot be resolved."""

    def __str__(self) -> str:
        # KeyError.__str__ would add quotes around the message; keep it clean.
        if len(self.args) == 1:
            return str(self.args[0])
        return super().__str__()


class SelectionError(ForensicSegmentsError, ValueError):
    """Raised when a requested byte range or preference list is invalid."""
