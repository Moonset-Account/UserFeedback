"""Exceptions raised by the forensic reader."""

from __future__ import annotations


class ForensicReaderError(Exception):
    """Base class for all errors in this package."""


class ChipValidationError(ForensicReaderError, ValueError):
    """Raised when chip dumps are missing or have incompatible lengths."""


class WiringConfigurationError(ForensicReaderError, ValueError):
    """Raised when candidate wiring or reversal settings are invalid."""


class EvidenceFormatError(ForensicReaderError, ValueError):
    """Raised when an evidence item is malformed or refers outside the image."""


class LogicalRangeError(ForensicReaderError, IndexError):
    """Raised when a requested logical interval is invalid."""


class NoConsistentWiringError(ForensicReaderError):
    """Raised by strict operations when no candidate wiring survives."""

    def __init__(self, message: str, *, version: int | None = None) -> None:
        super().__init__(message)
        self.version = version


class AmbiguousReadError(ForensicReaderError):
    """Raised when a single confirmed byte string is requested for an ambiguous read."""

    def __init__(self, message: str, report: object | None = None) -> None:
        super().__init__(message)
        self.report = report
