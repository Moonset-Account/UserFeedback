"""Range reader for objects compressed as independent raw-DEFLATE frames.

Public API:

* Data model: :class:`FrameEntry`, :class:`IndexVersion`, :class:`Index`.
* Storage: :class:`CompressedStore`, :class:`InMemoryStore`,
  :class:`FetchRecord`.
* Caching: :class:`DecompressedCache`, :class:`CacheKey`, :class:`CacheStats`.
* Reading: :class:`RangeReader`, :class:`PinnedSession`, :class:`RangeResult`.
* Errors: :class:`RangeDeflateError`, :class:`InvalidIndexError`,
  :class:`RangeOutOfBoundsError`, :class:`FrameTruncatedError`,
  :class:`FrameChecksumError`.
* Test-fixture builders: :mod:`range_deflate.builder`.

Only the Python standard library is required (Python 3.11).
"""

from .cache import CacheKey, CacheStats, DecompressedCache
from .errors import (
    FrameChecksumError,
    FrameError,
    FrameTruncatedError,
    InvalidIndexError,
    RangeDeflateError,
    RangeOutOfBoundsError,
)
from .models import FrameEntry, Index, IndexVersion
from .reader import PinnedSession, RangeReader, RangeResult
from .storage import CompressedStore, FetchRecord, InMemoryStore

__all__ = [
    "CacheKey",
    "CacheStats",
    "CompressedStore",
    "DecompressedCache",
    "FetchRecord",
    "FrameChecksumError",
    "FrameEntry",
    "FrameError",
    "FrameTruncatedError",
    "Index",
    "IndexVersion",
    "InMemoryStore",
    "InvalidIndexError",
    "PinnedSession",
    "RangeDeflateError",
    "RangeOutOfBoundsError",
    "RangeReader",
    "RangeResult",
]
