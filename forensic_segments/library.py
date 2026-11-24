"""Registration and extraction logic for the image segment library."""

from __future__ import annotations

from collections import OrderedDict
from typing import Dict, List, Optional, Sequence, Tuple

from .errors import SelectionError, UnknownSourceError, ValidationError
from .models import (
    Candidate,
    FillNote,
    Image,
    Segment,
    SegmentSelection,
    SegmentStatus,
    SelectionReport,
)


def _merge_ranges(ranges: "List[Tuple[int, int]]") -> "Tuple[Tuple[int, int], ...]":
    """Sort and coalesce adjacent or overlapping half-open ranges."""
    if not ranges:
        return ()
    ordered = sorted(ranges)
    merged: "List[List[int]]" = [list(ordered[0])]
    for start, end in ordered[1:]:
        last = merged[-1]
        if start <= last[1]:
            last[1] = max(last[1], end)
        else:
            merged.append([start, end])
    return tuple((s, e) for s, e in merged)


class ImageLibrary:
    """A registry of images of one evidence disk plus selection helpers.

    Images are grouped by ``capture_id`` (the original acquisition).  All files
    that copy one capture — whether declared through ``derived_from`` or simply
    sharing the capture id — are one *source*.  Only confirmed reads are
    referenceable; fill bytes keep offsets but never qualify as evidence.
    """

    def __init__(self) -> None:
        self._images: "OrderedDict[str, Image]" = OrderedDict()
        self._segment_size: Optional[int] = None

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------
    def add_image(self, image: Image) -> None:
        """Register one image file.

        Rules enforced here so later reports stay trustworthy:

        * Image ids are unique.  Re-adding an identical image is a no-op.
        * Every image of the library must use the same segment size, otherwise
          byte offsets could not be compared between images.
        * ``derived_from`` must name an already-registered image.  A declared
          copy must reproduce the parent's capture id, segment size and every
          segment exactly; a "copy" that differs is rejected rather than
          silently treated as a new source.
        """
        if not isinstance(image, Image):
            raise ValidationError("image must be an Image")

        existing = self._images.get(image.image_id)
        if existing is not None:
            if existing == image:
                return
            raise ValidationError(
                f"image {image.image_id!r} is already registered with different content"
            )

        if self._segment_size is None:
            self._segment_size = image.segment_size
        elif image.segment_size != self._segment_size:
            raise ValidationError(
                f"image {image.image_id!r} uses segment size {image.segment_size}, "
                f"but the library uses {self._segment_size}; offsets would not be comparable"
            )

        if image.derived_from is not None:
            parent = self._images.get(image.derived_from)
            if parent is None:
                raise UnknownSourceError(
                    f"image {image.image_id!r} derives from unknown image "
                    f"{image.derived_from!r}; add the parent first"
                )
            if image.capture_id != parent.capture_id:
                raise ValidationError(
                    f"image {image.image_id!r} derives from {parent.image_id!r} but "
                    f"declares capture {image.capture_id!r} instead of {parent.capture_id!r}"
                )
            if image.device_identity != parent.device_identity:
                raise ValidationError(
                    f"image {image.image_id!r} derives from {parent.image_id!r} but "
                    "declares a different device identity"
                )
            if image.segment_size != parent.segment_size or not image.same_content_as(parent):
                raise ValidationError(
                    f"image {image.image_id!r} is declared as a copy of "
                    f"{parent.image_id!r} but its segments differ"
                )

        self._images[image.image_id] = image

    def add_images(self, images: Sequence[Image]) -> None:
        for image in images:
            self.add_image(image)

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------
    @property
    def images(self) -> "Tuple[Image, ...]":
        return tuple(self._images.values())

    @property
    def image_ids(self) -> "Tuple[str, ...]":
        return tuple(self._images)

    @property
    def segment_size(self) -> Optional[int]:
        return self._segment_size

    def capture_ids(self) -> "Tuple[str, ...]":
        """Distinct original captures in registration order (deduplicated)."""
        ordered: "List[str]" = []
        for image in self._images.values():
            if image.capture_id not in ordered:
                ordered.append(image.capture_id)
        return tuple(ordered)

    def device_identities(self) -> "Tuple[str, ...]":
        """Distinct device identities reported by the registered images.

        Repeated imaging of one disk may be recorded under different serial or
        write-blocker identities; both are kept so the report can name the
        device that actually supplied each byte.
        """
        ordered: "List[str]" = []
        for image in self._images.values():
            if image.device_identity not in ordered:
                ordered.append(image.device_identity)
        return tuple(ordered)

    def images_for_device(self, device_identity: str) -> "Tuple[Image, ...]":
        found = tuple(
            img for img in self._images.values() if img.device_identity == device_identity
        )
        if not found:
            raise UnknownSourceError(f"unknown device identity {device_identity!r}")
        return found

    def images_for_capture(self, capture_id: str) -> "Tuple[Image, ...]":
        found = tuple(img for img in self._images.values() if img.capture_id == capture_id)
        if not found:
            raise UnknownSourceError(f"unknown capture {capture_id!r}")
        return found

    def image(self, image_id: str) -> Image:
        try:
            return self._images[image_id]
        except KeyError:
            raise UnknownSourceError(f"unknown image {image_id!r}") from None

    def segment_candidates(self, segment_index: int) -> "Tuple[Candidate, ...]":
        """Every image's state for one segment, in registration order."""
        if not isinstance(segment_index, int) or isinstance(segment_index, bool):
            raise SelectionError("segment_index must be an integer")
        if segment_index < 0:
            raise SelectionError("segment_index must be non-negative")
        priority = {image_id: rank for rank, image_id in enumerate(self._images)}
        return tuple(
            self._candidate_for(img, segment_index, priority)
            for img in self._images.values()
        )

    def confirmed_captures(self, segment_index: int) -> "Tuple[str, ...]":
        """Distinct original captures with a confirmed read of one segment.

        Copies of one capture are counted once, which is what makes this the
        honest "how many independent sources agree" measure.
        """
        seen: "List[str]" = []
        for cand in self.segment_candidates(segment_index):
            if cand.eligible and cand.capture_id not in seen:
                seen.append(cand.capture_id)
        return tuple(seen)

    def fill_images(self, segment_index: int) -> "Tuple[str, ...]":
        """Images that hold a fill (not evidence) at one segment."""
        return tuple(
            cand.image_id
            for cand in self.segment_candidates(segment_index)
            if cand.status is SegmentStatus.UNREADABLE
        )

    @staticmethod
    def _candidate_for(
        image: Image, segment_index: int, priority: "Dict[str, int]"
    ) -> Candidate:
        rank = priority.get(image.image_id, len(priority))
        seg: Optional[Segment] = image.segment_at(segment_index)
        if seg is None:
            return Candidate(
                image_id=image.image_id,
                capture_id=image.capture_id,
                device_identity=image.device_identity,
                status=SegmentStatus.NOT_IMAGED,
                rank=rank,
            )
        return Candidate(
            image_id=image.image_id,
            capture_id=image.capture_id,
            device_identity=image.device_identity,
            status=seg.status,
            digest=seg.digest,
            fill_byte=seg.fill_byte,
            rank=rank,
        )

    # ------------------------------------------------------------------
    # Extraction
    # ------------------------------------------------------------------
    def extract(
        self,
        start: int,
        end: int,
        *,
        preferred: Optional[Sequence[str]] = None,
    ) -> SelectionReport:
        """Extract bytes ``[start, end)`` and record the exact source used.

        ``preferred`` orders image ids (or nothing, for registration order).
        Copies of one original capture collapse: only the first listed file of
        a capture can win.  Unpreferred captures remain available as fallback
        for ranges the preferred ones did not confirm.
        """
        self._validate_range(start, end)
        warnings: "List[str]" = []
        priority = self._build_priority(preferred, warnings)

        if start == end:
            return SelectionReport(
                requested_start=start,
                requested_end=end,
                warnings=tuple(warnings),
            )

        size = self._segment_size
        assert size is not None  # images exist, otherwise range validation failed
        first_index = start // size
        last_index = (end - 1) // size

        segments: "List[SegmentSelection]" = []
        available: "List[Tuple[int, int]]" = []
        missing: "List[Tuple[int, int]]" = []
        conflicts: "List[Tuple[int, int]]" = []
        fill_notes: "List[FillNote]" = []

        seen_capture_digest_mismatch: "List[str]" = []

        for index in range(first_index, last_index + 1):
            seg_start = index * size
            seg_end = seg_start + size
            clipped_start = max(start, seg_start)
            clipped_end = min(end, seg_end)

            candidates = tuple(
                self._candidate_for(img, index, priority) for img in self._images.values()
            )
            candidates = tuple(sorted(candidates, key=lambda c: (c.rank, c.image_id)))

            # Best eligible image per capture, honouring preference order.
            per_capture: "OrderedDict[str, Candidate]" = OrderedDict()
            for cand in candidates:
                if not cand.eligible:
                    continue
                if cand.capture_id not in per_capture:
                    per_capture[cand.capture_id] = cand

            # Warn when files of a single capture contradict each other.
            for capture_id in per_capture:
                digests = {
                    c.digest
                    for c in candidates
                    if c.eligible and c.capture_id == capture_id
                }
                if len(digests) > 1:
                    seen_capture_digest_mismatch.append(
                        f"capture {capture_id!r} has conflicting digests at segment {index}"
                    )

            # Fill bytes keep their position.  Record every fill so the gap is
            # auditable even when another image later recovered those bytes.
            for cand in candidates:
                if cand.status is SegmentStatus.UNREADABLE:
                    fill_notes.append(
                        FillNote(
                            start=clipped_start,
                            end=clipped_end,
                            segment_index=index,
                            image_id=cand.image_id,
                            capture_id=cand.capture_id,
                            fill_byte=cand.fill_byte,  # type: ignore[arg-type]
                            covered=bool(per_capture),
                        )
                    )

            if not per_capture:
                missing.append((clipped_start, clipped_end))
                continue

            # A range is contested when eligible reads disagree at all.  Count
            # distinct digests (not files), so copies of one capture still
            # count as one source, while a capture that contradicts itself is
            # still surfaced as contested.
            distinct_digests = {
                cand.digest for cand in candidates if cand.eligible
            }
            conflicted = len(distinct_digests) > 1

            winner_capture = next(iter(per_capture))
            winner = per_capture[winner_capture]
            offset_in_segment = clipped_start - seg_start
            file_offset = index * size + offset_in_segment

            segments.append(
                SegmentSelection(
                    start=clipped_start,
                    end=clipped_end,
                    segment_index=index,
                    segment_start=seg_start,
                    segment_end=seg_end,
                    offset_in_segment=offset_in_segment,
                    file_offset=file_offset,
                    image_id=winner.image_id,
                    capture_id=winner.capture_id,
                    device_identity=winner.device_identity,
                    digest=winner.digest,  # type: ignore[arg-type]
                    conflicted=conflicted,
                    candidates=candidates,
                )
            )

            # The three categories partition the request: available (confirmed,
            # unanimous), conflict (confirmed but contested), missing (no read).
            if conflicted:
                conflicts.append((clipped_start, clipped_end))
            else:
                available.append((clipped_start, clipped_end))

        if seen_capture_digest_mismatch:
            warnings.extend(sorted(set(seen_capture_digest_mismatch)))

        available_ranges = _merge_ranges(available)
        missing_ranges = _merge_ranges(missing)
        conflict_ranges = _merge_ranges(conflicts)

        if conflict_ranges:
            warnings.append(
                "confirmed reads disagree on "
                + ", ".join(f"[{s}, {e})" for s, e in conflict_ranges)
                + "; the recorded image is one interpretation, review all candidates"
            )
        if missing_ranges:
            warnings.append(
                "no confirmed read covers "
                + ", ".join(f"[{s}, {e})" for s, e in missing_ranges)
            )

        segments.sort(key=lambda s: s.start)

        return SelectionReport(
            requested_start=start,
            requested_end=end,
            segments=tuple(segments),
            available_ranges=available_ranges,
            missing_ranges=missing_ranges,
            conflict_ranges=conflict_ranges,
            fill_notes=tuple(fill_notes),
            warnings=tuple(warnings),
        )

    def select_segment(
        self,
        segment_index: int,
        *,
        preferred: Optional[Sequence[str]] = None,
    ) -> SegmentSelection:
        """Convenience wrapper returning the selection for one whole segment."""
        if not isinstance(segment_index, int) or isinstance(segment_index, bool):
            raise SelectionError("segment_index must be an integer")
        if segment_index < 0:
            raise SelectionError("segment_index must be non-negative")
        size = self._segment_size
        if size is None:
            raise SelectionError("library contains no images")
        report = self.extract(
            segment_index * size, (segment_index + 1) * size, preferred=preferred
        )
        if not report.segments:
            raise SelectionError(
                f"segment {segment_index} has no confirmed read in any image"
            )
        return report.segments[0]

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _validate_range(self, start: int, end: int) -> None:
        for name, value in (("start", start), ("end", end)):
            if not isinstance(value, int) or isinstance(value, bool):
                raise SelectionError(f"{name} must be an integer")
            if value < 0:
                raise SelectionError(f"{name} must be non-negative")
        if end < start:
            raise SelectionError("end must be greater than or equal to start")
        if start == end:
            return
        if not self._images:
            raise SelectionError("library contains no images")

    def _build_priority(
        self,
        preferred: Optional[Sequence[str]],
        warnings: "List[str]",
    ) -> "Dict[str, int]":
        """Map each image id to a selection rank.

        Explicitly listed images rank first, in listed order.  Copies of an
        already-ranked capture are dropped with a warning: the copy is the same
        source and cannot widen coverage.  Everything else falls back to
        registration order.
        """
        priority: "Dict[str, int]" = {}
        if preferred is None:
            for rank, image_id in enumerate(self._images):
                priority[image_id] = rank
            return priority

        if isinstance(preferred, str):
            raise SelectionError("preferred must be a sequence of image ids, not a string")

        seen_captures: "set[str]" = set()
        rank = 0
        for image_id in preferred:
            if not isinstance(image_id, str) or not image_id.strip():
                raise SelectionError("preferred entries must be non-empty strings")
            image = self.image(image_id)
            if image_id in priority:
                warnings.append(f"preference {image_id!r} is listed more than once; ignored")
                continue
            if image.capture_id in seen_captures:
                warnings.append(
                    f"preference {image_id!r} copies an already-preferred capture "
                    f"{image.capture_id!r}; duplicates count as one source and were ignored"
                )
                continue
            seen_captures.add(image.capture_id)
            priority[image_id] = rank
            rank += 1

        for image_id, image in self._images.items():
            if image_id in priority:
                continue
            if image.capture_id in seen_captures:
                # Other files of an already-preferred capture: same source.
                continue
            priority[image_id] = rank
            rank += 1

        # Files of a preferred capture that were not listed still must resolve;
        # give them the worst rank so a listed file always wins.  The capture
        # they belong to has already been ordered, so this cannot widen sources.
        for image_id in self._images:
            if image_id not in priority:
                priority[image_id] = rank
                rank += 1
        return priority
