"""Exception types shared by the screening service and client."""

from __future__ import annotations

from typing import Optional


class ScreeningError(Exception):
    """Base class for every error raised by this package."""


class DigestError(ScreeningError, ValueError):
    """A digest, password or digest list is malformed."""


class InvalidPrefix(DigestError):
    """A lookup prefix is malformed or has the wrong length."""


class VersionError(ScreeningError):
    """A version or page cannot be served."""


class NoCurrentVersion(VersionError):
    """The store has not published any version yet."""


class VersionUnavailable(VersionError):
    """The requested version is no longer retained."""

    def __init__(self, requested: str, current: Optional["DigestVersion"] = None) -> None:
        self.requested = requested
        self.current = current
        message = f"version {requested!r} is no longer available"
        if current is not None:
            message += f"; current version is {current.version_id!r}"
        super().__init__(message)


class PageOutOfRange(VersionError):
    """The requested page does not exist in the pinned version."""

    def __init__(self, page: int, total_pages: int) -> None:
        self.page = page
        self.total_pages = total_pages
        super().__init__(f"page {page} out of range for a {total_pages}-page version")


class ProtocolError(ScreeningError):
    """The peer sent a response that violates the screening contract."""


class RestartRequired(ProtocolError):
    """The pinned version can no longer be read atomically from page 0.

    The client must discard every page it has read for the old version and
    start again against ``current_version``.  Partial data from two versions
    must never be combined.
    """

    def __init__(self, reason: str, current_version: Optional[str] = None) -> None:
        self.reason = reason
        self.current_version = current_version
        message = f"restart required: {reason}"
        if current_version is not None:
            message += f" (current version {current_version!r})"
        super().__init__(message)
