"""Exception hierarchy for the range-deflate component.

All errors carry the identifier of the frame whose processing failed so
callers can pinpoint the exact frame in a dependency chain. No partial
range data is ever returned when one of these is raised.
"""

from __future__ import annotations


class RangeDeflateError(Exception):
    """Base class for every error raised by this package."""


class InvalidIndexError(RangeDeflateError):
    """An index version is structurally invalid (gaps, cycles, bad dict)."""


class RangeOutOfBoundsError(RangeDeflateError):
    """A requested logical byte range does not fit inside the object."""


class FrameError(RangeDeflateError):
    """Base class for failures while materialising a specific frame."""

    def __init__(self, frame_id: str, message: str):
        self.frame_id = frame_id
        full = f"frame {frame_id!r}: {message}"
        super().__init__(full)


class FrameTruncatedError(FrameError):
    """The compressed store could not supply all bytes of the frame.

    This covers both a store that ends early and any underlying read
    failure reported by the storage backend.
    """


class FrameChecksumError(FrameError):
    """A frame could not be inflated, produced a bad length, or its
    decompressed checksum did not match the value recorded in the index.

    A wrong or missing reset dictionary normally surfaces here because the
    inflated bytes differ from the recorded checksum (or inflate itself
    rejects an out-of-window back-reference).
    """
