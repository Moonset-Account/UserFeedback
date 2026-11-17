"""Core data model: page fragments, VA->file map, snapshot-scoped byte reads.

Acquisition model
-----------------
A memory acquisition yields *page fragments*.  Each fragment is tagged with
the process generation and the snapshot id of the acquisition run, carries
the virtual address (VA) it was captured at, the captured bytes, and an
``origin`` label identifying the segment file it came from.

A separate VA->file-offset map describes where virtual addresses live in
the segment files, so every byte read can report its source interval.

Merge rules
-----------
* Fragments are only ever combined inside one ``(generation, snapshot_id)``
  bucket.  Cross-snapshot bytes are never assembled into one object.
* Inside a bucket, overlapping fragments merge only where the overlapping
  bytes are identical.  Where they differ the read reports a conflict and
  keeps *both* candidate byte strings together with *both* source
  intervals.
* VAs with no covering fragment report ``missing_page``; a fragment that
  genuinely contains zero bytes reports ``ok`` with value zero.  The two
  are never conflated.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass


class FieldStatus(enum.Enum):
    """Per-field (and per-byte-read) outcome."""

    OK = "ok"
    MISSING_PAGE = "missing_page"
    CONFLICT = "conflict"
    OUT_OF_BOUNDS = "out_of_bounds"
    DEPENDENCY_FAILED = "dependency_failed"


@dataclass(frozen=True)
class MapEntry:
    """One VA range -> file offset range mapping."""

    va_start: int
    length: int
    file_offset: int

    @property
    def va_end(self) -> int:
        return self.va_start + self.length

    def covers(self, va: int, size: int) -> bool:
        return self.va_start <= va and va + size <= self.va_end

    def to_file_offset(self, va: int) -> int:
        return self.file_offset + (va - self.va_start)


@dataclass(frozen=True)
class SourceInterval:
    """Provenance of a byte run: where it lives in the segment file."""

    file_offset: int
    length: int
    generation: int
    snapshot_id: str
    origin: str

    @property
    def file_end(self) -> int:
        return self.file_offset + self.length


@dataclass(frozen=True)
class PageFragment:
    """One captured page fragment."""

    generation: int
    snapshot_id: str
    va_start: int
    data: bytes
    origin: str = "<unnamed>"

    @property
    def va_end(self) -> int:
        return self.va_start + len(self.data)


@dataclass(frozen=True)
class ConflictCandidate:
    """One of several disagreeing byte strings for the same VA run."""

    data: bytes
    sources: tuple[SourceInterval, ...]


@dataclass(frozen=True)
class ConflictSegment:
    """A VA run where same-snapshot fragments disagree."""

    va_start: int
    length: int
    candidates: tuple[ConflictCandidate, ...]


@dataclass(frozen=True)
class ByteRead:
    """Result of reading ``size`` bytes at a VA inside one snapshot."""

    status: FieldStatus
    data: bytes | None
    sources: tuple[SourceInterval, ...]
    missing: tuple[tuple[int, int], ...]  # VA intervals with no coverage
    conflicts: tuple[ConflictSegment, ...]


class SnapshotStore:
    """Fragment store keyed by (generation, snapshot_id).

    Read-only: fragments can be added, never mutated or removed.
    """

    def __init__(self, va_map: list[MapEntry]):
        self._entries = tuple(sorted(va_map, key=lambda e: e.va_start))
        for a, b in zip(self._entries, self._entries[1:]):
            if b.va_start < a.va_end:
                raise ValueError("overlapping VA map entries")
        self._fragments: dict[tuple[int, str], list[PageFragment]] = {}

    def _entry_for(self, va: int, size: int) -> MapEntry | None:
        for e in self._entries:
            if e.covers(va, size):
                return e
        return None

    def add_fragment(self, frag: PageFragment) -> None:
        if not frag.data:
            raise ValueError("empty fragment")
        if self._entry_for(frag.va_start, len(frag.data)) is None:
            raise ValueError(
                f"fragment 0x{frag.va_start:x}..0x{frag.va_end:x} "
                "not covered by the VA map"
            )
        key = (frag.generation, frag.snapshot_id)
        self._fragments.setdefault(key, []).append(frag)

    def snapshots(self) -> list[tuple[int, str]]:
        return sorted(self._fragments)

    def _source_for(self, frag: PageFragment, a: int, b: int) -> SourceInterval:
        entry = self._entry_for(a, b - a)
        assert entry is not None  # fragments are validated on add
        return SourceInterval(
            file_offset=entry.to_file_offset(a),
            length=b - a,
            generation=frag.generation,
            snapshot_id=frag.snapshot_id,
            origin=frag.origin,
        )

    def read_bytes(
        self, generation: int, snapshot_id: str, va: int, size: int
    ) -> ByteRead:
        """Read ``size`` bytes at ``va`` using only one snapshot's fragments."""
        if size < 0:
            raise ValueError("negative size")
        if size == 0:
            return ByteRead(FieldStatus.OK, b"", (), (), ())
        end = va + size
        frags = [
            f
            for f in self._fragments.get((generation, snapshot_id), ())
            if f.va_start < end and f.va_end > va
        ]
        points = {va, end}
        for f in frags:
            points.add(max(f.va_start, va))
            points.add(min(f.va_end, end))
        pts = sorted(points)

        missing: list[tuple[int, int]] = []
        conflicts: list[ConflictSegment] = []
        data_parts: list[bytes] = []
        sources: list[SourceInterval] = []
        for a, b in zip(pts, pts[1:]):
            cand: dict[bytes, list[PageFragment]] = {}
            for f in frags:
                if f.va_start <= a and f.va_end >= b:
                    off = a - f.va_start
                    chunk = bytes(f.data[off : off + (b - a)])
                    cand.setdefault(chunk, []).append(f)
            if not cand:
                missing.append((a, b))
                continue
            if len(cand) > 1:
                conflicts.append(
                    ConflictSegment(
                        va_start=a,
                        length=b - a,
                        candidates=tuple(
                            ConflictCandidate(
                                data=chunk,
                                sources=tuple(
                                    self._source_for(f, a, b) for f in fs
                                ),
                            )
                            for chunk, fs in cand.items()
                        ),
                    )
                )
                continue
            chunk, fs = next(iter(cand.items()))
            data_parts.append(chunk)
            src = self._source_for(fs[0], a, b)
            if (
                sources
                and sources[-1].file_end == src.file_offset
                and sources[-1].origin == src.origin
                and sources[-1].generation == src.generation
                and sources[-1].snapshot_id == src.snapshot_id
            ):
                prev = sources[-1]
                sources[-1] = SourceInterval(
                    prev.file_offset,
                    prev.length + src.length,
                    src.generation,
                    src.snapshot_id,
                    src.origin,
                )
            else:
                sources.append(src)

        if missing or conflicts:
            # If both occur, missing_page is the headline status; the
            # conflict detail is still preserved in the result.
            status = FieldStatus.MISSING_PAGE if missing else FieldStatus.CONFLICT
            return ByteRead(status, None, tuple(sources), tuple(missing), tuple(conflicts))
        return ByteRead(FieldStatus.OK, b"".join(data_parts), tuple(sources), (), ())
