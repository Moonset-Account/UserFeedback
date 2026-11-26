"""A tiny deterministic pixel-grid frame buffer.

Each cell is a one-character stand-in for an arbitrary physical pixel.  ``None``
means that the cell must not be published in the current patch set.  This makes
publish-region tests exact without requiring an imaging dependency.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Iterable, Sequence

from .geometry import Rect


class PixelGrid:
    """Rectangular character grid with optional unpublished cells."""

    def __init__(
        self,
        width: int,
        height: int,
        fill: str | None = ".",
    ) -> None:
        if not isinstance(width, int) or not isinstance(height, int):
            raise TypeError("width and height must be integers")
        if width < 0 or height < 0:
            raise ValueError("width and height must be non-negative")
        if fill is not None and len(fill) != 1:
            raise ValueError("fill must be one character or None")
        self.width = width
        self.height = height
        self._cells: list[list[str | None]] = [
            [fill for _ in range(width)] for _ in range(height)
        ]

    @classmethod
    def from_rows(cls, rows: Sequence[str | Sequence[str | None]]) -> "PixelGrid":
        if not rows:
            return cls(0, 0)
        width = len(rows[0])
        if any(len(row) != width for row in rows):
            raise ValueError("all rows must have the same width")
        grid = cls(width, len(rows), fill=None)
        grid._cells = [list(row) for row in rows]
        return grid

    def copy(self) -> "PixelGrid":
        clone = PixelGrid(self.width, self.height, fill=None)
        clone._cells = deepcopy(self._cells)
        return clone

    def in_bounds(self, x: int, y: int) -> bool:
        return 0 <= x < self.width and 0 <= y < self.height

    def get(self, x: int, y: int) -> str | None:
        if not self.in_bounds(x, y):
            raise IndexError(f"pixel ({x}, {y}) is outside {self.width}x{self.height}")
        return self._cells[y][x]

    def set(self, x: int, y: int, value: str | None) -> None:
        if not self.in_bounds(x, y):
            raise IndexError(f"pixel ({x}, {y}) is outside {self.width}x{self.height}")
        if value is not None and len(value) != 1:
            raise ValueError("pixel value must be one character or None")
        self._cells[y][x] = value

    def fill_rect(self, rect: Rect, value: str | None) -> None:
        """Fill the intersection of ``rect`` with this grid."""

        x0 = max(0, int(rect.x))
        y0 = max(0, int(rect.y))
        x1 = min(self.width, int(rect.right))
        y1 = min(self.height, int(rect.bottom))
        if x1 <= x0 or y1 <= y0:
            return
        if value is not None and len(value) != 1:
            raise ValueError("pixel value must be one character or None")
        for y in range(y0, y1):
            for x in range(x0, x1):
                self._cells[y][x] = value

    def clear_rect(self, rect: Rect) -> None:
        """Mark cells as unavailable for publication."""

        self.fill_rect(rect, None)

    def extract(self, rect: Rect) -> "PixelGrid":
        """Extract an integer pixel rectangle."""

        if (
            rect.x < 0
            or rect.y < 0
            or rect.right > self.width
            or rect.bottom > self.height
        ):
            raise ValueError(f"extract rectangle {rect} exceeds grid {self.size}")
        if float(rect.x) != int(rect.x) or float(rect.y) != int(rect.y):
            raise TypeError("extract requires integer pixel coordinates")
        x0, y0 = int(rect.x), int(rect.y)
        width, height = int(rect.width), int(rect.height)
        patch = PixelGrid(width, height, fill=None)
        for y in range(height):
            for x in range(width):
                patch._cells[y][x] = self._cells[y0 + y][x0 + x]
        return patch

    def rows(self, none_value: str | None = None) -> list[str]:
        """Return text rows.

        If ``none_value`` is ``None``, unpublished cells raise ``ValueError``.
        Use :meth:`render` for a displayable representation of held cells.
        """

        result: list[str] = []
        for row in self._cells:
            chars: list[str] = []
            for cell in row:
                if cell is None:
                    if none_value is None:
                        raise ValueError("grid contains unpublished/None cells")
                    chars.append(none_value)
                else:
                    chars.append(cell)
            result.append("".join(chars))
        return result

    def render(self, hold_char: str = "?") -> list[str]:
        return self.rows(none_value=hold_char)

    @property
    def size(self) -> tuple[int, int]:
        return self.width, self.height

    def published_cell_count(self) -> int:
        return sum(cell is not None for row in self._cells for cell in row)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, PixelGrid) and self._cells == other._cells

    def __repr__(self) -> str:
        return f"PixelGrid({self.width!r}, {self.height!r})"
