"""Immutable index model: frame entries, versions and append-only index."""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock
from typing import List, Optional, Sequence

from .errors import InvalidIndexError


@dataclass(frozen=True)
class FrameEntry:
    """One raw-DEFLATE frame and everything needed to inflate it.

    Attributes:
        frame_id: Unique identifier of the frame within an index version.
            The cache keys frames by their physical compressed location plus
            checksum, so different versions may reuse the same id freely.
        logical_start: First logical (uncompressed) byte of the frame,
            measured from the start of the whole object.
        logical_end: One past the last logical byte of the frame.
        compressed_offset: Start of the raw DEFLATE byte string inside the
            compressed store.
        compressed_length: Number of compressed bytes the frame occupies.
        checksum: CRC-32 of the fully decompressed frame bytes.
        dict_frame_id: Identifier of the predecessor frame whose tail bytes
            form this frame's reset dictionary, or ``None`` for the first
            frame / a frame with no external dictionary.
        dict_size: Number of tail bytes taken from ``dict_frame_id``'s
            decompressed output. Must be zero exactly when
            ``dict_frame_id is None``.
    """

    frame_id: str
    logical_start: int
    logical_end: int
    compressed_offset: int
    compressed_length: int
    checksum: int
    dict_frame_id: Optional[str] = None
    dict_size: int = 0

    @property
    def logical_length(self) -> int:
        return self.logical_end - self.logical_start


@dataclass(frozen=True)
class IndexVersion:
    """An immutable snapshot of a complete frame layout.

    Versions are numbered from zero and are never mutated once published.
    """

    version: int
    entries: tuple[FrameEntry, ...]

    def entry_by_id(self, frame_id: str) -> FrameEntry:
        for entry in self.entries:
            if entry.frame_id == frame_id:
                return entry
        raise InvalidIndexError(
            f"unknown frame id {frame_id!r} in version {self.version}"
        )


def _validate_version(version: int, entries: Sequence[FrameEntry]) -> None:
    if not entries:
        raise InvalidIndexError(
            f"version {version} must contain at least one frame"
        )

    seen: set[str] = set()
    by_id: dict[str, FrameEntry] = {}
    for entry in entries:
        if not isinstance(entry.frame_id, str) or not entry.frame_id:
            raise InvalidIndexError("frame ids must be non-empty strings")
        if entry.frame_id in seen:
            raise InvalidIndexError(
                f"duplicate frame id {entry.frame_id!r} in version {version}"
            )
        seen.add(entry.frame_id)
        by_id[entry.frame_id] = entry

        for name, value in (
            ("logical_start", entry.logical_start),
            ("logical_end", entry.logical_end),
            ("compressed_offset", entry.compressed_offset),
            ("compressed_length", entry.compressed_length),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise InvalidIndexError(
                    f"frame {entry.frame_id!r}: {name} must be a non-negative int"
                )
        if entry.logical_end <= entry.logical_start:
            raise InvalidIndexError(
                f"frame {entry.frame_id!r}: logical range must be non-empty"
            )
        if entry.compressed_length == 0:
            raise InvalidIndexError(
                f"frame {entry.frame_id!r}: compressed length must be positive"
            )
        if not isinstance(entry.checksum, int) or not 0 <= entry.checksum <= 0xFFFFFFFF:
            raise InvalidIndexError(
                f"frame {entry.frame_id!r}: checksum must be a 32-bit value"
            )
        if entry.dict_size < 0 or entry.dict_size > 0xFFFFFFFF:
            raise InvalidIndexError(
                f"frame {entry.frame_id!r}: dict_size out of range"
            )
        if entry.dict_frame_id is None:
            if entry.dict_size != 0:
                raise InvalidIndexError(
                    f"frame {entry.frame_id!r}: dict_size must be 0 without a "
                    "dictionary frame"
                )
        else:
            if entry.dict_size == 0:
                raise InvalidIndexError(
                    f"frame {entry.frame_id!r}: dict_size must be positive when "
                    "a dictionary frame is referenced"
                )

    # Logical ranges must be contiguous, non-overlapping and start at zero.
    ordered = sorted(entries, key=lambda e: e.logical_start)
    cursor = 0
    for entry in ordered:
        if entry.logical_start != cursor:
            raise InvalidIndexError(
                f"frame {entry.frame_id!r}: logical gap or overlap at byte "
                f"{cursor} (frame starts at {entry.logical_start})"
            )
        cursor = entry.logical_end

    # Dictionary references must resolve, be acyclic and the requested tail
    # must actually exist in the predecessor's decompressed output.
    state: dict[str, int] = {}  # 0 = visiting, 1 = done

    def visit(frame_id: str, stack: tuple[str, ...]) -> None:
        status = state.get(frame_id)
        if status == 1:
            return
        if status == 0:
            chain = " -> ".join(stack + (frame_id,))
            raise InvalidIndexError(f"dictionary dependency cycle: {chain}")
        entry = by_id[frame_id]
        state[frame_id] = 0
        if entry.dict_frame_id is not None:
            dep = by_id.get(entry.dict_frame_id)
            if dep is None:
                raise InvalidIndexError(
                    f"frame {frame_id!r}: dictionary frame "
                    f"{entry.dict_frame_id!r} does not exist"
                )
            if entry.dict_size > dep.logical_length:
                raise InvalidIndexError(
                    f"frame {frame_id!r}: needs {entry.dict_size} dictionary "
                    f"bytes but frame {dep.frame_id!r} only decompresses to "
                    f"{dep.logical_length} bytes"
                )
            visit(entry.dict_frame_id, stack + (frame_id,))
        state[frame_id] = 1

    for frame_id in by_id:
        visit(frame_id, ())


class Index:
    """Append-only collection of immutable index versions.

    New versions are added with :meth:`append_version`; existing versions
    are never modified. A read always pins a concrete version snapshot, so
    an append during a read cannot change what that read observes.
    """

    def __init__(self, entries: Optional[Sequence[FrameEntry]] = None):
        self._lock = RLock()
        self._versions: List[IndexVersion] = []
        if entries is not None:
            self.append_version(entries)

    def append_version(self, entries: Sequence[FrameEntry]) -> IndexVersion:
        """Validate and publish a new version; returns the snapshot."""
        entries = tuple(entries)
        with self._lock:
            version = len(self._versions)
            _validate_version(version, entries)
            snapshot = IndexVersion(version=version, entries=entries)
            self._versions.append(snapshot)
            return snapshot

    @property
    def latest_version(self) -> int:
        with self._lock:
            if not self._versions:
                raise InvalidIndexError("index contains no versions")
            return len(self._versions) - 1

    def get_version(self, version: Optional[int] = None) -> IndexVersion:
        """Return a pinned snapshot; ``None`` means the current latest."""
        with self._lock:
            if not self._versions:
                raise InvalidIndexError("index contains no versions")
            if version is None:
                return self._versions[-1]
            if not isinstance(version, int) or isinstance(version, bool):
                raise InvalidIndexError("version must be an int or None")
            if version < 0 or version >= len(self._versions):
                raise InvalidIndexError(
                    f"unknown version {version}; have 0..{len(self._versions) - 1}"
                )
            return self._versions[version]

    def __len__(self) -> int:
        with self._lock:
            return len(self._versions)
