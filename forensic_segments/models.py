"""Immutable value objects shared by the library and its reports."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple

from .errors import ValidationError


class SegmentStatus(str, Enum):
    """How one fixed-size slice of an image was obtained."""

    READ = "read"
    """The imaging tool confirmed a read; a digest describes the bytes."""

    UNREADABLE = "unreadable"
    """The tool tried the range, the medium failed, and it substituted a fill.

    The fill keeps later byte offsets aligned, but the bytes are not evidence
    and can never be referenced.
    """

    NOT_IMAGED = "not_imaged"
    """The imaging run stopped before reaching this range."""


def _require_id(name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{name} must be a non-empty string")
    return value


def _require_int(name: str, value: object, *, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValidationError(f"{name} must be an integer")
    if value < minimum:
        raise ValidationError(f"{name} must be >= {minimum}")
    return value


@dataclass(frozen=True)
class Segment:
    """A fixed-size slice of one image with its read state and content digest.

    ``digest`` is an opaque, caller-supplied fingerprint (for example a hex
    SHA-256).  The library compares digests exactly; it never interprets them.
    """

    index: int
    status: SegmentStatus
    digest: Optional[str] = None
    fill_byte: Optional[int] = None

    def __post_init__(self) -> None:
        _require_int("segment index", self.index)
        try:
            status = SegmentStatus(self.status)
        except (ValueError, TypeError):
            raise ValidationError(
                f"segment {self.index}: unknown status {self.status!r}"
            ) from None
        object.__setattr__(self, "status", status)

        if status is SegmentStatus.READ:
            if not isinstance(self.digest, str) or not self.digest.strip():
                raise ValidationError(
                    f"segment {self.index}: a read segment requires a non-empty digest"
                )
            if self.fill_byte is not None:
                raise ValidationError(
                    f"segment {self.index}: a read segment cannot carry a fill byte"
                )
        elif status is SegmentStatus.UNREADABLE:
            if not isinstance(self.fill_byte, int) or isinstance(self.fill_byte, bool):
                raise ValidationError(
                    f"segment {self.index}: an unreadable segment requires an integer fill_byte"
                )
            if not 0 <= self.fill_byte <= 0xFF:
                raise ValidationError(
                    f"segment {self.index}: fill_byte must be between 0 and 255"
                )
            if self.digest is not None:
                raise ValidationError(
                    f"segment {self.index}: a fill-filled segment cannot carry a digest"
                )
        else:  # NOT_IMAGED
            if self.digest is not None or self.fill_byte is not None:
                raise ValidationError(
                    f"segment {self.index}: a not-imaged segment carries no content"
                )

    @property
    def is_evidence(self) -> bool:
        """True only for confirmed reads; fills and gaps never qualify."""
        return self.status is SegmentStatus.READ

    @property
    def requires_fill(self) -> bool:
        return self.status is SegmentStatus.UNREADABLE


@dataclass(frozen=True)
class Image:
    """One file that resulted from imaging the evidence disk.

    ``capture_id`` names the *original capture* the bytes came from.  Two files
    that merely copy the same original capture share it and therefore count as
    a single source, even when both files exist under different names.

    ``derived_from`` optionally records the image id this file was copied from.
    A declared copy must reproduce its parent exactly; otherwise the metadata is
    rejected, because a "copy" that changed bytes is not a copy.
    """

    image_id: str
    device_identity: str
    capture_id: str
    segment_size: int
    segments: Tuple[Segment, ...]
    derived_from: Optional[str] = None

    def __post_init__(self) -> None:
        _require_id("image_id", self.image_id)
        _require_id("device_identity", self.device_identity)
        _require_id("capture_id", self.capture_id)
        _require_int("segment_size", self.segment_size, minimum=1)
        if self.derived_from is not None:
            _require_id("derived_from", self.derived_from)
            if self.derived_from == self.image_id:
                raise ValidationError(
                    f"image {self.image_id!r} cannot derive from itself"
                )
        if not isinstance(self.segments, (tuple, list)):
            raise ValidationError("segments must be a sequence of Segment values")
        segments = tuple(self.segments)
        object.__setattr__(self, "segments", segments)
        expected = 0
        for seg in segments:
            if not isinstance(seg, Segment):
                raise ValidationError("segments must contain Segment values")
            if seg.index != expected:
                raise ValidationError(
                    f"image {self.image_id!r}: segment indexes must be contiguous "
                    f"from 0; expected {expected}, got {seg.index}"
                )
            expected += 1

    @property
    def segment_count(self) -> int:
        return len(self.segments)

    @property
    def byte_length(self) -> int:
        return self.segment_count * self.segment_size

    def segment_for_offset(self, offset: int) -> Optional[Segment]:
        """Return the segment covering ``offset``, or None when out of range."""
        if offset < 0 or offset >= self.byte_length:
            return None
        return self.segments[offset // self.segment_size]

    def segment_at(self, index: int) -> Optional[Segment]:
        if 0 <= index < self.segment_count:
            return self.segments[index]
        return None

    def same_content_as(self, other: "Image") -> bool:
        return self.segment_size == other.segment_size and self.segments == other.segments


@dataclass(frozen=True)
class Candidate:
    """One image's state for a specific segment, retained for review."""

    image_id: str
    capture_id: str
    device_identity: str
    status: SegmentStatus
    digest: Optional[str] = None
    fill_byte: Optional[int] = None
    rank: int = 0

    @property
    def eligible(self) -> bool:
        """Only a confirmed read with content may be referenced as evidence."""
        return self.status is SegmentStatus.READ and self.digest is not None


@dataclass(frozen=True)
class FillNote:
    """A fill region that occupies requested bytes but is not evidence.

    ``covered`` is True when another acquisition's confirmed read supplied the
    same bytes; the fill then documents a recovered gap rather than a loss.
    """

    start: int
    end: int
    segment_index: int
    image_id: str
    capture_id: str
    fill_byte: int
    covered: bool = False

    @property
    def length(self) -> int:
        return self.end - self.start


@dataclass(frozen=True)
class SegmentSelection:
    """The image and offsets actually chosen for one contiguous sub-range.

    ``file_offset`` is the byte offset inside ``image_id``.  Because segments
    map one-to-one onto logical image offsets, it equals the logical offset, but
    it is reported separately so a reviewer can address the exact file.
    """

    start: int
    end: int
    segment_index: int
    segment_start: int
    segment_end: int
    offset_in_segment: int
    file_offset: int
    image_id: str
    capture_id: str
    device_identity: str
    digest: str
    conflicted: bool
    candidates: Tuple[Candidate, ...] = ()

    @property
    def length(self) -> int:
        return self.end - self.start

    @property
    def file_offset_end(self) -> int:
        return self.file_offset + self.length

    @property
    def corroborating_captures(self) -> Tuple[str, ...]:
        """Original captures with at least one confirmed read matching the choice."""
        by_capture = self._digests_by_capture()
        return tuple(
            capture_id
            for capture_id, digests in by_capture.items()
            if self.digest in digests
        )

    @property
    def dissenting_captures(self) -> Tuple[str, ...]:
        """Original captures with at least one confirmed read differing from it."""
        by_capture = self._digests_by_capture()
        return tuple(
            capture_id
            for capture_id, digests in by_capture.items()
            if any(digest != self.digest for digest in digests)
        )

    def _digests_by_capture(self) -> "dict[str, set[str]]":
        by_capture: "dict[str, set[str]]" = {}
        for cand in self.candidates:
            if cand.eligible:
                by_capture.setdefault(cand.capture_id, set()).add(cand.digest)  # type: ignore[arg-type]
        return by_capture


@dataclass(frozen=True)
class SelectionReport:
    """Everything an investigator needs to review one extraction.

    ``available_ranges``, ``conflict_ranges`` and ``missing_ranges`` partition
    the requested range: available and conflict ranges both have a recorded
    image and offset, but only available ranges are backed by unanimous
    confirmed reads.
    """

    requested_start: int
    requested_end: int
    segments: Tuple[SegmentSelection, ...] = ()
    available_ranges: Tuple[Tuple[int, int], ...] = ()
    conflict_ranges: Tuple[Tuple[int, int], ...] = ()
    missing_ranges: Tuple[Tuple[int, int], ...] = ()
    fill_notes: Tuple[FillNote, ...] = ()
    warnings: Tuple[str, ...] = ()

    @property
    def length(self) -> int:
        return self.requested_end - self.requested_start

    @property
    def available_length(self) -> int:
        return sum(e - s for s, e in self.available_ranges)

    @property
    def conflict_length(self) -> int:
        return sum(e - s for s, e in self.conflict_ranges)

    @property
    def missing_length(self) -> int:
        return sum(e - s for s, e in self.missing_ranges)

    @property
    def covered_length(self) -> int:
        """Bytes backed by at least one confirmed read (available or contested)."""
        return self.available_length + self.conflict_length

    @property
    def selected_length(self) -> int:
        """Bytes for which a concrete image and file offset were recorded."""
        return sum(seg.length for seg in self.segments)

    @property
    def coverage(self) -> float:
        """Fraction of requested bytes backed by at least one confirmed read."""
        if self.length == 0:
            return 1.0
        return self.covered_length / self.length

    @property
    def is_complete(self) -> bool:
        """True when every requested byte is available and uncontested."""
        return not self.missing_ranges and not self.conflict_ranges

    def covering(self, offset: int) -> Optional[SegmentSelection]:
        for seg in self.segments:
            if seg.start <= offset < seg.end:
                return seg
        return None

    def segments_for_image(self, image_id: str) -> Tuple[SegmentSelection, ...]:
        return tuple(seg for seg in self.segments if seg.image_id == image_id)

    def to_dict(self) -> dict:
        return {
            "requested": {
                "start": self.requested_start,
                "end": self.requested_end,
                "length": self.length,
            },
            "summary": {
                "available_length": self.available_length,
                "conflict_length": self.conflict_length,
                "missing_length": self.missing_length,
                "covered_length": self.covered_length,
                "selected_length": self.selected_length,
                "coverage": self.coverage,
                "is_complete": self.is_complete,
            },
            "available_ranges": [list(r) for r in self.available_ranges],
            "conflict_ranges": [list(r) for r in self.conflict_ranges],
            "missing_ranges": [list(r) for r in self.missing_ranges],
            "segments": [
                {
                    "start": s.start,
                    "end": s.end,
                    "length": s.length,
                    "segment_index": s.segment_index,
                    "offset_in_segment": s.offset_in_segment,
                    "image_id": s.image_id,
                    "file_offset": s.file_offset,
                    "file_offset_end": s.file_offset_end,
                    "capture_id": s.capture_id,
                    "device_identity": s.device_identity,
                    "digest": s.digest,
                    "conflicted": s.conflicted,
                    "corroborating_captures": list(s.corroborating_captures),
                    "dissenting_captures": list(s.dissenting_captures),
                    "candidates": [
                        {
                            "image_id": c.image_id,
                            "capture_id": c.capture_id,
                            "device_identity": c.device_identity,
                            "status": c.status.value,
                            "digest": c.digest,
                            "fill_byte": c.fill_byte,
                            "eligible": c.eligible,
                            "rank": c.rank,
                        }
                        for c in s.candidates
                    ],
                }
                for s in self.segments
            ],
            "fill_notes": [
                {
                    "start": f.start,
                    "end": f.end,
                    "segment_index": f.segment_index,
                    "image_id": f.image_id,
                    "capture_id": f.capture_id,
                    "fill_byte": f.fill_byte,
                    "covered": f.covered,
                }
                for f in self.fill_notes
            ],
            "warnings": list(self.warnings),
        }

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    def render(self) -> str:
        """Human-readable review sheet for a second investigator.

        Rows are emitted per selected sub-range so the recorded image and file
        offset of every byte are visible; missing ranges are added separately.
        ``available_ranges`` / ``conflict_ranges`` aggregate adjacent segments,
        so they are not used for this listing.
        """
        lines = [
            f"Requested bytes [{self.requested_start}, {self.requested_end}) "
            f"({self.length} bytes)",
            f"Available {self.available_length}  Conflict {self.conflict_length}  "
            f"Missing {self.missing_length}  Coverage {self.coverage:.1%}",
        ]
        if self.length:
            lines.append("")
            rows: "list[tuple[int, int, int, object]]" = []
            for seg in self.segments:
                rows.append((seg.start, seg.end, 0, seg))
            for start, end in self.missing_ranges:
                rows.append((start, end, 1, None))
            for start, end, kind, payload in sorted(rows, key=lambda r: (r[0], r[2])):
                if kind == 1:
                    fills = [f for f in self.fill_notes if f.start == start and f.end == end]
                    detail = "no confirmed read"
                    if fills:
                        detail += "; fill " + ", ".join(
                            f"{f.image_id}({f.capture_id})=0x{f.fill_byte:02x}"
                            for f in fills
                        )
                    lines.append(f"  [{start}, {end})  {'missing':<9} {detail}")
                    continue
                seg = payload
                assert isinstance(seg, SegmentSelection)
                label = "conflict" if seg.conflicted else "available"
                detail = (
                    f"image={seg.image_id} capture={seg.capture_id} "
                    f"device={seg.device_identity} segment={seg.segment_index} "
                    f"file_offset={seg.file_offset} digest={seg.digest}"
                )
                if seg.conflicted:
                    others = sorted(
                        {
                            f"{c.image_id}({c.capture_id})={c.digest}"
                            for c in seg.candidates
                            if c.eligible and c.digest != seg.digest
                        }
                    )
                    detail += "; differs from " + ", ".join(others)
                lines.append(f"  [{start}, {end})  {label:<9} {detail}")
        if self.warnings:
            lines.append("")
            lines.append("Warnings:")
            lines.extend(f"  - {w}" for w in self.warnings)
        return "\n".join(lines)
