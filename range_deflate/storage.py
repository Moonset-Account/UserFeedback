"""Storage backends that serve raw compressed frame bytes."""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock
from typing import List, Optional, Protocol, runtime_checkable

from .errors import FrameTruncatedError
from .models import FrameEntry


@dataclass(frozen=True)
class FetchRecord:
    """One physical byte fetch, attributed to a frame.

    The same frame is recorded each time its compressed bytes are read from
    the store. Cache hits do not produce a record.
    """

    frame_id: str
    offset: int
    length: int


@runtime_checkable
class CompressedStore(Protocol):
    """Minimal interface a storage backend must implement."""

    def read(self, offset: int, length: int) -> bytes:
        """Return exactly ``length`` bytes starting at ``offset``.

        Implementations should raise
        :class:`~range_deflate.errors.FrameTruncatedError` when the requested
        slice cannot be fully served; any other exception is wrapped by the
        reader.
        """


class InMemoryStore:
    """Byte-buffer store that also records every frame fetch.

    The fetch log is the audit trail a caller uses to verify which frames a
    range read actually touched (see :class:`FetchRecord`).
    """

    def __init__(self, data: bytes = b""):
        self._data = bytes(data)
        self._lock = RLock()
        self._log: List[FetchRecord] = []

    @property
    def size(self) -> int:
        return len(self._data)

    def read(self, offset: int, length: int) -> bytes:
        if offset < 0 or length < 0:
            raise ValueError("offset and length must be non-negative")
        end = offset + length
        with self._lock:
            if end > len(self._data):
                raise FrameTruncatedError(
                    "<unknown>",
                    f"store has {len(self._data)} bytes; "
                    f"need [{offset}, {end})",
                )
            return self._data[offset:end]

    def fetch_frame(self, entry: FrameEntry) -> bytes:
        """Read a whole frame and append an attributed fetch record."""
        with self._lock:
            try:
                payload = self.read(
                    entry.compressed_offset, entry.compressed_length
                )
            except FrameTruncatedError as exc:
                raise FrameTruncatedError(
                    entry.frame_id,
                    f"compressed slice "
                    f"[{entry.compressed_offset}, "
                    f"{entry.compressed_offset + entry.compressed_length}) "
                    f"is truncated ({exc})",
                ) from None
            self._log.append(
                FetchRecord(
                    frame_id=entry.frame_id,
                    offset=entry.compressed_offset,
                    length=entry.compressed_length,
                )
            )
            return payload

    @property
    def fetch_log(self) -> tuple[FetchRecord, ...]:
        with self._lock:
            return tuple(self._log)

    def fetched_frame_ids(self) -> tuple[str, ...]:
        """Identifiers of frames physically fetched, in fetch order."""
        with self._lock:
            return tuple(record.frame_id for record in self._log)

    def fetch_count(self, frame_id: Optional[str] = None) -> int:
        with self._lock:
            if frame_id is None:
                return len(self._log)
            return sum(1 for record in self._log if record.frame_id == frame_id)

    def reset_fetch_log(self) -> None:
        with self._lock:
            self._log.clear()
