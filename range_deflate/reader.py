"""Range reader: logical byte ranges over indexed raw-DEFLATE frames."""

from __future__ import annotations

from dataclasses import dataclass
from types import TracebackType
from typing import Dict, List, Optional, Sequence, Tuple, Type

from .cache import CacheKey, DecompressedCache
from .errors import (
    FrameChecksumError,
    FrameError,
    FrameTruncatedError,
    InvalidIndexError,
    RangeOutOfBoundsError,
)
from .inflate import inflate_frame
from .models import FrameEntry, Index, IndexVersion
from .storage import CompressedStore, InMemoryStore


@dataclass(frozen=True)
class RangeResult:
    """Outcome of one range read.

    Attributes:
        data: Requested logical bytes (never partial: an error is raised
            instead if any required frame fails).
        version: Index version pinned for this read.
        frames_needed: Every frame materialised for the read, in dependency
            order. Includes dictionary-only predecessors that do not
            intersect the requested logical interval.
        frames_fetched: Frames whose compressed bytes were physically read
            from the store (cache misses), in fetch order.
        frames_from_cache: Frames served from the decompressed cache.
        logical_coverage: Frames intersecting the requested interval.
    """

    data: bytes
    version: int
    frames_needed: tuple[str, ...]
    frames_fetched: tuple[str, ...]
    frames_from_cache: tuple[str, ...]
    logical_coverage: tuple[str, ...]


class PinnedSession:
    """A view on the reader pinned to one immutable index version."""

    def __init__(self, reader: "RangeReader", version: int):
        self._reader = reader
        self._snapshot = reader._index.get_version(version)

    @property
    def version(self) -> int:
        return self._snapshot.version

    def read_range(
        self, start: int, end: Optional[int] = None
    ) -> RangeResult:
        return self._reader._read_range(self._snapshot, start, end)

    def close(self) -> None:
        # Stateless today, but the context-manager API is part of the
        # contract so callers can rely on it.
        return None

    def __enter__(self) -> "PinnedSession":
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        self.close()


class RangeReader:
    """Reads logical byte ranges from a versioned raw-DEFLATE object."""

    def __init__(
        self,
        index: Index,
        store: CompressedStore,
        cache: Optional[DecompressedCache] = None,
    ):
        self._index = index
        self._store = store
        self._cache = cache if cache is not None else DecompressedCache(None)

    @property
    def cache(self) -> DecompressedCache:
        return self._cache

    def session(self, version: Optional[int] = None) -> PinnedSession:
        """Pin a version for several reads; ``None`` pins current latest."""
        snapshot = self._index.get_version(version)
        return PinnedSession(self, snapshot.version)

    def read_range(
        self,
        start: int,
        end: Optional[int] = None,
        version: Optional[int] = None,
    ) -> RangeResult:
        """Read logical bytes ``[start, end)`` from one pinned version.

        The version snapshot is resolved before any frame is touched, so an
        append racing with this call cannot change the version mid-read.
        """
        snapshot = self._index.get_version(version)
        return self._read_range(snapshot, start, end)

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    def _object_length(self, snapshot: IndexVersion) -> int:
        return max(entry.logical_end for entry in snapshot.entries)

    def _read_range(
        self, snapshot: IndexVersion, start: int, end: Optional[int]
    ) -> RangeResult:
        if not isinstance(start, int) or isinstance(start, bool) or start < 0:
            raise RangeOutOfBoundsError("start must be a non-negative int")
        object_length = self._object_length(snapshot)
        if end is None:
            end = object_length
        if not isinstance(end, int) or isinstance(end, bool) or end < 0:
            raise RangeOutOfBoundsError("end must be a non-negative int")
        if end < start:
            raise RangeOutOfBoundsError(
                f"end ({end}) precedes start ({start})"
            )
        if end > object_length or start > object_length:
            raise RangeOutOfBoundsError(
                f"range [{start}, {end}) exceeds object length {object_length}"
            )

        by_id: Dict[str, FrameEntry] = {
            entry.frame_id: entry for entry in snapshot.entries
        }
        ordered = sorted(snapshot.entries, key=lambda e: e.logical_start)

        coverage = [
            entry
            for entry in ordered
            if entry.logical_start < end and entry.logical_end > start
        ]

        # Empty range: nothing to fetch or inflate.
        if start == end or not coverage:
            return RangeResult(
                data=b"",
                version=snapshot.version,
                frames_needed=(),
                frames_fetched=(),
                frames_from_cache=(),
                logical_coverage=(),
            )

        needed = self._collect_chain(by_id, coverage)
        inflated, fetched, cached = self._materialise(by_id, needed)

        chunks: List[bytes] = []
        for entry in coverage:
            payload = inflated[entry.frame_id]
            lo = max(start, entry.logical_start) - entry.logical_start
            hi = min(end, entry.logical_end) - entry.logical_start
            chunks.append(payload[lo:hi])

        return RangeResult(
            data=b"".join(chunks),
            version=snapshot.version,
            frames_needed=tuple(entry.frame_id for entry in needed),
            frames_fetched=tuple(fetched),
            frames_from_cache=tuple(cached),
            logical_coverage=tuple(entry.frame_id for entry in coverage),
        )

    def _collect_chain(
        self,
        by_id: Dict[str, FrameEntry],
        coverage: Sequence[FrameEntry],
    ) -> List[FrameEntry]:
        """Return coverage frames plus dictionary deps in dependency order.

        Depth-first post-order guarantees every dictionary predecessor is
        fully inflated before the frame that needs it. Cycles and unknown
        references cannot occur here because the index validated them, but
        defensive checks keep the failure mode explicit.
        """
        ordered: List[FrameEntry] = []
        placed: set[str] = set()

        def place(entry: FrameEntry, stack: Tuple[str, ...]) -> None:
            if entry.frame_id in placed:
                return
            if entry.frame_id in stack:
                raise InvalidIndexError(
                    "dictionary dependency cycle: "
                    + " -> ".join(stack + (entry.frame_id,))
                )
            if entry.dict_frame_id is not None:
                dep = by_id.get(entry.dict_frame_id)
                if dep is None:
                    raise InvalidIndexError(
                        f"frame {entry.frame_id!r} references missing "
                        f"dictionary frame {entry.dict_frame_id!r}"
                    )
                place(dep, stack + (entry.frame_id,))
            ordered.append(entry)
            placed.add(entry.frame_id)

        for entry in coverage:
            place(entry, ())
        return ordered

    def _materialise(
        self,
        by_id: Dict[str, FrameEntry],
        needed: Sequence[FrameEntry],
    ) -> Tuple[Dict[str, bytes], List[str], List[str]]:
        """Inflate every needed frame; no partial result survives an error."""
        inflated: Dict[str, bytes] = {}
        fetched: List[str] = []
        cached: List[str] = []

        for entry in needed:
            key = CacheKey.from_entry(entry)
            payload = self._cache.get(key)
            if payload is not None:
                cached.append(entry.frame_id)
                inflated[entry.frame_id] = payload
                continue

            dictionary = self._build_dictionary(entry, by_id, inflated)
            compressed = self._fetch(entry)
            payload = inflate_frame(
                frame_id=entry.frame_id,
                compressed=compressed,
                expected_checksum=entry.checksum,
                expected_length=entry.logical_length,
                dictionary=dictionary,
            )
            inflated[entry.frame_id] = payload
            fetched.append(entry.frame_id)
            self._cache.put(key, payload)

        return inflated, fetched, cached

    def _build_dictionary(
        self,
        entry: FrameEntry,
        by_id: Dict[str, FrameEntry],
        inflated: Dict[str, bytes],
    ) -> Optional[bytes]:
        if entry.dict_frame_id is None:
            return None
        predecessor = inflated.get(entry.dict_frame_id)
        if predecessor is None:
            # The dependency-order walk should make this unreachable.
            raise FrameChecksumError(
                entry.frame_id,
                f"dictionary frame {entry.dict_frame_id!r} was not inflated "
                "before this frame",
            )
        if entry.dict_size > len(predecessor):
            raise FrameChecksumError(
                entry.frame_id,
                f"needs {entry.dict_size} dictionary bytes but predecessor "
                f"{entry.dict_frame_id!r} holds {len(predecessor)}",
            )
        return predecessor[-entry.dict_size:]

    def _fetch(self, entry: FrameEntry) -> bytes:
        if isinstance(self._store, InMemoryStore):
            return self._store.fetch_frame(entry)
        try:
            return self._store.read(
                entry.compressed_offset, entry.compressed_length
            )
        except FrameTruncatedError:
            raise
        except FrameError:
            raise
        except Exception as exc:  # pragma: no cover - backend-specific
            raise FrameTruncatedError(
                entry.frame_id, f"storage backend failed: {exc}"
            ) from None
