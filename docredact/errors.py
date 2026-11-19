"""Error types raised by :mod:`docredact`."""

from __future__ import annotations


class DocError(ValueError):
    """Raised when a document, path or export request is invalid."""


class RedactionError(DocError):
    """Raised when a redaction request cannot be honoured."""


__all__ = ["DocError", "RedactionError"]
