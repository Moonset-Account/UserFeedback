"""Data model for the read-only MIME locator.

All offsets are absolute byte offsets into the original, unmodified source
message.  Nothing in this module ever mutates the supplied source bytes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple


class MimeParseError(ValueError):
    """Raised when the source bytes do not form a parseable MIME message."""


class InvalidSelectionError(ValueError):
    """Raised when a decoded-byte selection is out of range or malformed."""


class NotSelectableError(TypeError):
    """Raised when selection is attempted on a non-leaf container part."""


class CorruptedPartError(InvalidSelectionError):
    """Raised when a selected part's transfer encoding is damaged.

    Sibling parts are not affected: each leaf is decoded independently.
    """


@dataclass(frozen=True, slots=True)
class PathEntry:
    """One hop in the hierarchy path from the root to a part."""

    kind: str  # "part" (multipart child) or "rfc822" (nested message)
    index: int  # zero-based position among the enclosing container's children
    content_type: str
    filename: Optional[str] = None


@dataclass(frozen=True, slots=True)
class Unit:
    """One original encoded evidence unit mapped to decoded bytes.

    For base64 a unit is a complete four-character group (a single selected
    decoded byte is therefore attributed to the whole group).  For
    quoted-printable and identity encodings a unit is a contiguous literal
    byte run; soft line breaks and multipart boundary lines are never part of
    any unit and are therefore never reported as evidence.
    """

    decoded_start: int
    decoded_end: int
    raw_ranges: Tuple[Tuple[int, int], ...]


@dataclass(slots=True)
class PartNode:
    """A node in the parsed MIME tree.

    Container nodes (``multipart/*`` and ``message/rfc822``) own children;
    leaf nodes own encoded units and may be selected against.
    """

    kind: str  # "multipart", "rfc822", or "leaf"
    content_type: str
    filename: Optional[str]
    path: Tuple[PathEntry, ...]
    leaf_index: Optional[int] = None
    children: List["PartNode"] = field(default_factory=list)
    units: List[Unit] = field(default_factory=list)
    decoded_length: int = 0
    encoding: Optional[str] = None
    raw_body_start: Optional[int] = None
    raw_body_end: Optional[int] = None
    corrupted: bool = False
    corruption_reason: Optional[str] = None

    @property
    def is_leaf(self) -> bool:
        return self.kind == "leaf"

    @property
    def is_corrupted(self) -> bool:
        return self.corrupted

    def path_string(self) -> str:
        """Human-readable path, e.g. ``root > part[0] > rfc822 > part[1]``."""
        if not self.path:
            return "root"
        return "root > " + " > ".join(
            f"{entry.kind}[{entry.index}]" for entry in self.path
        )


@dataclass(frozen=True, slots=True)
class Location:
    """Result of locating a decoded-byte selection."""

    path: Tuple[PathEntry, ...]
    leaf_index: int
    content_type: str
    filename: Optional[str]
    encoding: str
    requested_start: int
    requested_end: int
    covered_start: int
    covered_end: int
    raw_ranges: Tuple[Tuple[int, int], ...]

    def path_string(self) -> str:
        if not self.path:
            return "root"
        return "root > " + " > ".join(
            f"{entry.kind}[{entry.index}]" for entry in self.path
        )

    def extract(self, source: bytes) -> bytes:
        """Re-read the original encoded bytes from the source.

        The source is only read, never modified.  Gaps between ranges (soft
        line breaks, boundary lines) are deliberately excluded.
        """
        return b"".join(source[start:end] for start, end in self.raw_ranges)


def merge_ranges(ranges: List[Tuple[int, int]]) -> Tuple[Tuple[int, int], ...]:
    """Merge adjacent/overlapping ``[start, end)`` ranges."""
    if not ranges:
        return ()
    ordered = sorted(ranges)
    merged: List[List[int]] = [list(ordered[0])]
    for start, end in ordered[1:]:
        if start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1][1] = end
        else:
            merged.append([start, end])
    return tuple((start, end) for start, end in merged)
