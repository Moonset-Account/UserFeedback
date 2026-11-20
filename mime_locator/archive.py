"""High-level read-only facade for locating selections in MIME messages."""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

from .model import (
    CorruptedPartError,
    InvalidSelectionError,
    Location,
    NotSelectableError,
    PartNode,
    PathEntry,
    merge_ranges,
)
from .parser import parse_message


class MimeArchive:
    """Parsed, immutable view of a MIME message.

    The archive keeps the original source bytes so that located ranges can be
    re-read and verified.  It never writes anywhere.
    """

    def __init__(self, source: bytes):
        if not isinstance(source, (bytes, bytearray, memoryview)):
            raise TypeError("source must be bytes-like")
        self._source = bytes(source)
        self._root = parse_message(self._source)
        self._leaves: List[PartNode] = []
        self._by_index: Dict[int, PartNode] = {}
        self._collect(self._root)

    @property
    def source(self) -> bytes:
        return self._source

    @property
    def root(self) -> PartNode:
        return self._root

    def _collect(self, node: PartNode) -> None:
        if node.is_leaf:
            assert node.leaf_index is not None
            self._leaves.append(node)
            self._by_index[node.leaf_index] = node
        for child in node.children:
            self._collect(child)

    def leaves(self) -> Tuple[PartNode, ...]:
        """All selectable leaf parts in depth-first order."""
        return tuple(self._leaves)

    def get_part(self, leaf_index: int) -> PartNode:
        try:
            return self._by_index[leaf_index]
        except KeyError:
            raise InvalidSelectionError(
                f"unknown leaf index {leaf_index}; "
                f"valid indices are 0..{len(self._leaves) - 1}"
            ) from None

    def find_parts(
        self,
        *,
        filename: Optional[str] = None,
        content_type: Optional[str] = None,
    ) -> Tuple[PartNode, ...]:
        """Find leaves by exact filename and/or content type.

        Duplicate attachments are returned as distinct parts (with distinct
        hierarchy paths), never collapsed.
        """
        results = []
        for leaf in self._leaves:
            if filename is not None and leaf.filename != filename:
                continue
            if content_type is not None and leaf.content_type != content_type:
                continue
            results.append(leaf)
        return tuple(results)

    def locate(
        self,
        leaf_index: int,
        start: int,
        end: Optional[int] = None,
    ) -> Location:
        """Locate a decoded-byte selection inside a leaf part.

        ``start`` and ``end`` are offsets into the final, fully decoded body
        bytes, half-open.  A zero-width selection (``start == end``) is legal
        and yields no raw ranges.  For base64, selected bytes are expanded to
        the complete four-character group(s) that encode them.
        """
        if not isinstance(start, int) or isinstance(start, bool):
            raise InvalidSelectionError("selection start must be an integer")
        if end is None:
            end = start
        if not isinstance(end, int) or isinstance(end, bool):
            raise InvalidSelectionError("selection end must be an integer")
        if start < 0 or end < 0:
            raise InvalidSelectionError(
                f"selection offsets must be non-negative, got [{start}, {end})"
            )
        if end < start:
            raise InvalidSelectionError(
                f"selection end {end} precedes start {start}"
            )

        leaf = self.get_part(leaf_index)
        if not leaf.is_leaf:
            raise NotSelectableError(
                f"part {leaf.path_string()} is a container and has no body"
            )
        if leaf.is_corrupted:
            raise CorruptedPartError(
                f"part {leaf.path_string()} cannot be located because its "
                f"encoding is corrupted: {leaf.corruption_reason}"
            )
        if start > leaf.decoded_length or end > leaf.decoded_length:
            raise InvalidSelectionError(
                f"selection [{start}, {end}) exceeds decoded length "
                f"{leaf.decoded_length} of part {leaf.path_string()}"
            )

        if start == end:
            selected_ranges: List[Tuple[int, int]] = []
            covered_start = covered_end = start
        else:
            selected_ranges = []
            covered_start = leaf.decoded_length
            covered_end = 0
            for unit in leaf.units:
                if unit.decoded_end <= start or unit.decoded_start >= end:
                    continue
                selected_ranges.extend(unit.raw_ranges)
                covered_start = min(covered_start, unit.decoded_start)
                covered_end = max(covered_end, unit.decoded_end)
            # A non-empty valid selection always intersects at least one unit.
            assert covered_end > 0

        assert leaf.encoding is not None
        return Location(
            path=leaf.path,
            leaf_index=leaf.leaf_index,
            content_type=leaf.content_type,
            filename=leaf.filename,
            encoding=leaf.encoding,
            requested_start=start,
            requested_end=end,
            covered_start=covered_start,
            covered_end=covered_end,
            raw_ranges=merge_ranges(selected_ranges),
        )

    def locate_path(
        self,
        path: Sequence[PathEntry],
        start: int,
        end: Optional[int] = None,
    ) -> Location:
        """Locate by hierarchy path instead of leaf index."""
        target = tuple(path)
        for leaf in self._leaves:
            if leaf.path == target:
                assert leaf.leaf_index is not None
                return self.locate(leaf.leaf_index, start, end)
        raise InvalidSelectionError(
            "no selectable leaf matches the given hierarchy path: "
            + " > ".join(f"{entry.kind}[{entry.index}]" for entry in target)
        )
