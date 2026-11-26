"""Small rectangle/region helpers used by the occlusion engine.

Coordinates may be floating-point logical coordinates.  Pixel-space rectangles
are represented by the same :class:`Rect` type, but their components are integers
after transformation/clipping.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Iterator, Sequence


@dataclass(frozen=True, slots=True)
class Rect:
    """An axis-aligned rectangle.

    ``x``/``y`` are the upper/left boundaries and ``width``/``height`` are the
    boundary extents.  Thus a pixel cell ``(px, py)`` intersects the half-open
    rectangle ``[x, x + width) × [y, y + height)``.
    """

    x: float = 0
    y: float = 0
    width: float = 0
    height: float = 0

    def __post_init__(self) -> None:
        if self.width < 0 or self.height < 0:
            raise ValueError("Rect width and height must be non-negative")

    @property
    def right(self) -> float:
        return self.x + self.width

    @property
    def bottom(self) -> float:
        return self.y + self.height

    @property
    def is_empty(self) -> bool:
        return self.width <= 0 or self.height <= 0

    def translated(self, dx: float, dy: float) -> "Rect":
        return Rect(self.x + dx, self.y + dy, self.width, self.height)

    def __iter__(self) -> Iterator[float]:
        yield self.x
        yield self.y
        yield self.width
        yield self.height


def intersection(a: Rect, b: Rect) -> Rect | None:
    """Return the area shared by two rectangles, or ``None``."""

    x1 = max(a.x, b.x)
    y1 = max(a.y, b.y)
    x2 = min(a.right, b.right)
    y2 = min(a.bottom, b.bottom)
    if x2 <= x1 or y2 <= y1:
        return None
    return Rect(x1, y1, x2 - x1, y2 - y1)


def intersects(a: Rect, b: Rect) -> bool:
    return intersection(a, b) is not None


def clip_rect(rect: Rect, bounds: Rect) -> Rect | None:
    """Clip ``rect`` to ``bounds``."""

    return intersection(rect, bounds)


def subtract_rect(a: Rect, hole: Rect) -> list[Rect]:
    """Return non-overlapping rectangles covering ``a - hole``.

    At most four rectangles are returned (top, bottom, left-middle and
    right-middle).  Zero-area pieces are omitted.
    """

    cut = intersection(a, hole)
    if cut is None:
        return [a]
    if cut == a:
        return []

    pieces = (
        Rect(a.x, a.y, a.width, cut.y - a.y),
        Rect(a.x, cut.bottom, a.width, a.bottom - cut.bottom),
        Rect(a.x, cut.y, cut.x - a.x, cut.height),
        Rect(cut.right, cut.y, a.right - cut.right, cut.height),
    )
    return [piece for piece in pieces if not piece.is_empty]


def subtract_many(rects: Iterable[Rect], hole: Rect) -> list[Rect]:
    """Subtract ``hole`` from every rectangle in ``rects``."""

    result: list[Rect] = []
    for rect in rects:
        result.extend(subtract_rect(rect, hole))
    return result


def subtract_all(rects: Iterable[Rect], holes: Iterable[Rect]) -> list[Rect]:
    """Subtract every hole from a rectangle region."""

    result = list(rects)
    for hole in holes:
        result = subtract_many(result, hole)
    return result


def full_pixel_rect(width: int, height: int) -> Rect:
    if width < 0 or height < 0:
        raise ValueError("pixel dimensions must be non-negative")
    return Rect(0, 0, width, height)


def as_int_rect(rect: Rect) -> Rect:
    """Floor the origin and ceil the far boundaries (conservative coverage)."""

    import math

    x0 = math.floor(min(rect.x, rect.right))
    y0 = math.floor(min(rect.y, rect.bottom))
    x1 = math.ceil(max(rect.x, rect.right))
    y1 = math.ceil(max(rect.y, rect.bottom))
    return Rect(x0, y0, x1 - x0, y1 - y0)


def normalize_pixel_rects(rects: Sequence[Rect]) -> list[Rect]:
    """Clip/drop invalid pixel rectangles and preserve input order.

    The engine normally subtracts rectangles instead of requiring a union.
    This helper is useful at API boundaries where negative/out-of-range damage
    rectangles may have been supplied.
    """

    normalized: list[Rect] = []
    for rect in rects:
        if rect.width < 0 or rect.height < 0:
            raise ValueError("pixel rectangles must have non-negative size")
        if not rect.is_empty:
            normalized.append(
                Rect(int(rect.x), int(rect.y), int(rect.width), int(rect.height))
            )
    return normalized
