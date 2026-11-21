"""Logical rectangles and coverage checks for checkpoint shards.

A :class:`Rect` is a half-open logical rectangle ``[row0, row1) x [col0, col1)``
inside a two-dimensional weight tensor.  Shards own one such rectangle each;
recovery has to prove that the union of the old shards intersecting a target
rectangle covers it exactly once (no gaps, no overlaps).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Sequence, Tuple, Union

__all__ = ["Rect", "cover_rect", "CoverReport"]


@dataclass(frozen=True)
class Rect:
    """Half-open logical rectangle in a rank-2 tensor."""

    row0: int
    col0: int
    row1: int
    col1: int

    def __post_init__(self) -> None:
        coords = (self.row0, self.col0, self.row1, self.col1)
        if any(not isinstance(v, int) for v in coords):
            raise TypeError("rect coordinates must be integers")
        if min(coords) < 0:
            raise ValueError("rect coordinates must be non-negative")
        if self.row1 < self.row0 or self.col1 < self.col0:
            raise ValueError("rect end must not precede its start")

    @classmethod
    def coerce(cls, value: Union["Rect", Sequence[int]]) -> "Rect":
        if isinstance(value, Rect):
            return value
        seq = tuple(value)
        if len(seq) != 4:
            raise ValueError("rect needs four coordinates [row0, col0, row1, col1]")
        return cls(*seq)

    @classmethod
    def full(cls, rows: int, cols: int) -> "Rect":
        return cls(0, 0, rows, cols)

    def as_tuple(self) -> Tuple[int, int, int, int]:
        return (self.row0, self.col0, self.row1, self.col1)

    @property
    def rows(self) -> int:
        return self.row1 - self.row0

    @property
    def cols(self) -> int:
        return self.col1 - self.col0

    @property
    def is_empty(self) -> bool:
        return self.row0 == self.row1 or self.col0 == self.col1

    def intersect(self, other: "Rect") -> "Rect":
        row0 = max(self.row0, other.row0)
        col0 = max(self.col0, other.col0)
        row1 = min(self.row1, other.row1)
        col1 = min(self.col1, other.col1)
        if row0 >= row1 or col0 >= col1:
            # An empty, canonicalised rectangle.
            return Rect(row0, col0, row0, col0)
        return Rect(row0, col0, row1, col1)

    def contains(self, other: "Rect") -> bool:
        return (
            self.row0 <= other.row0
            and self.col0 <= other.col0
            and other.row1 <= self.row1
            and other.col1 <= self.col1
        )

    def __str__(self) -> str:
        return f"Rect(rows[{self.row0}:{self.row1}], cols[{self.col0}:{self.col1}])"


@dataclass(frozen=True)
class CoverReport:
    """Result of checking whether pieces tile ``target`` exactly once."""

    ok: bool
    uncovered: Tuple[Rect, ...]
    # Each overlap entry is (piece_a, piece_b, intersection).
    overlaps: Tuple[Tuple[Rect, Rect, Rect], ...]


def cover_rect(pieces: Sequence[Rect], target: Rect) -> CoverReport:
    """Verify that ``pieces`` cover ``target`` exactly once.

    The pieces are clipped to ``target`` first, so pieces extending outside
    the target are harmless.  A horizontal band sweep is used so that both
    row-wise and column-wise shard layouts (and arbitrary mixtures) work.
    """

    target = Rect.coerce(target)
    clipped: List[Rect] = []
    for piece in pieces:
        part = Rect.coerce(piece).intersect(target)
        if not part.is_empty:
            clipped.append(part)

    if target.is_empty:
        return CoverReport(True, (), ())

    # Band boundaries: every piece edge inside the target.
    edges = {target.row0, target.row1}
    for part in clipped:
        edges.add(part.row0)
        edges.add(part.row1)
    band_rows = sorted(edges)

    uncovered: List[Rect] = []
    overlaps: List[Tuple[Rect, Rect, Rect]] = []

    for top, bottom in zip(band_rows, band_rows[1:]):
        if top < target.row0 or bottom > target.row1:
            continue
        band_pieces = [p for p in clipped if p.row0 <= top and p.row1 >= bottom]
        intervals = sorted(
            ((p.col0, p.col1, p) for p in band_pieces),
            key=lambda item: (item[0], item[1]),
        )

        cursor = target.col0
        prev_end = target.col0
        prev_piece: Rect | None = None

        for c0, c1, piece in intervals:
            # Gap detection runs against the furthest column covered so far.
            if c0 > cursor:
                uncovered.append(Rect(top, cursor, bottom, c0))
            # Overlap detection runs against the immediately preceding
            # interval, independent of any gap that may precede it.
            if prev_piece is not None and c0 < prev_end:
                overlap = Rect(top, c0, bottom, min(c1, prev_end))
                overlaps.append((prev_piece, piece, overlap))
            prev_end = c1
            prev_piece = piece
            cursor = max(cursor, c1)

        if cursor < target.col1:
            uncovered.append(Rect(top, cursor, bottom, target.col1))

    return CoverReport(not uncovered and not overlaps, tuple(uncovered), tuple(overlaps))
