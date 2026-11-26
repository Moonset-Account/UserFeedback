"""Display topology, DPI scaling and rotation transforms."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping, Sequence

from .geometry import Rect, intersection


class TopologyVersionUnavailable(KeyError):
    """Raised when an explicitly requested topology version is absent."""


class FrameDimensionError(ValueError):
    """Raised when a frame does not match its display topology."""


SUPPORTED_ROTATIONS = (0, 90, 180, 270)


@dataclass(frozen=True, slots=True)
class Display:
    """One captured display.

    ``bounds`` use the same global logical desktop coordinate system as window
    rectangles.  ``rotation`` is the clockwise transform from that logical
    display rectangle into the raw captured pixel grid:

    * ``0``: raw grid is ``bounds.size * scale``.
    * ``90`` / ``270``: raw width and height are swapped.
    * ``180``: dimensions are unchanged.

    Window rectangles are first clipped to ``bounds`` and then mapped into this
    display's raw pixel grid.  If the platform reports the inverse rotation,
    pass ``(-reported_rotation) % 360``.
    """

    id: str
    bounds: Rect
    scale: float = 1.0
    rotation: int = 0
    scale_y: float | None = None
    pixel_width: int | None = None
    pixel_height: int | None = None

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("display id must not be empty")
        if self.scale <= 0:
            raise ValueError("scale must be positive")
        sy = self.scale if self.scale_y is None else self.scale_y
        if sy <= 0:
            raise ValueError("scale_y must be positive")
        object.__setattr__(self, "scale_y", sy)

        rotation = self.rotation % 360
        if rotation not in SUPPORTED_ROTATIONS:
            raise ValueError("rotation must be one of 0, 90, 180, 270 degrees")
        object.__setattr__(self, "rotation", rotation)

        if (self.pixel_width is None) != (self.pixel_height is None):
            raise ValueError("pixel_width and pixel_height must be supplied together")
        if self.pixel_width is not None and (
            self.pixel_width <= 0 or self.pixel_height <= 0  # type: ignore[operator]
        ):
            raise ValueError("explicit pixel dimensions must be positive")

        oriented_w = self.bounds.width * self.scale
        oriented_h = self.bounds.height * sy
        if rotation in (90, 270):
            expected_w, expected_h = math.ceil(oriented_h), math.ceil(oriented_w)
        else:
            expected_w, expected_h = math.ceil(oriented_w), math.ceil(oriented_h)
        if self.pixel_width is not None and (
            self.pixel_width != expected_w or self.pixel_height != expected_h
        ):
            raise ValueError(
                "explicit pixel dimensions "
                f"{self.pixel_width}x{self.pixel_height} do not match "
                f"scale/rotation-derived {expected_w}x{expected_h}"
            )

    @property
    def effective_scale_y(self) -> float:
        assert self.scale_y is not None
        return self.scale_y

    @property
    def oriented_pixel_width(self) -> float:
        return self.bounds.width * self.scale

    @property
    def oriented_pixel_height(self) -> float:
        return self.bounds.height * self.effective_scale_y

    @property
    def raw_pixel_width(self) -> int:
        if self.pixel_width is not None:
            return self.pixel_width
        if self.rotation in (90, 270):
            return math.ceil(self.oriented_pixel_height)
        return math.ceil(self.oriented_pixel_width)

    @property
    def raw_pixel_height(self) -> int:
        if self.pixel_height is not None:
            return self.pixel_height
        if self.rotation in (90, 270):
            return math.ceil(self.oriented_pixel_width)
        return math.ceil(self.oriented_pixel_height)

    @property
    def pixel_bounds(self) -> Rect:
        return Rect(0, 0, self.raw_pixel_width, self.raw_pixel_height)

    def logical_to_pixel_rect(self, global_rect: Rect) -> Rect | None:
        """Map and clip a global logical rect to raw frame pixel boundaries.

        Fractional source boundaries are expanded outward (floor origin, ceil
        far edge), ensuring that partially covered protected pixels are never
        left unmasked.
        """

        clipped = intersection(global_rect, self.bounds)
        if clipped is None or clipped.is_empty:
            return None

        local_x = clipped.x - self.bounds.x
        local_y = clipped.y - self.bounds.y

        x0 = local_x * self.scale
        y0 = local_y * self.effective_scale_y
        x1 = (local_x + clipped.width) * self.scale
        y1 = (local_y + clipped.height) * self.effective_scale_y

        oriented_w = self.oriented_pixel_width
        oriented_h = self.oriented_pixel_height

        corners = ((x0, y0), (x1, y0), (x1, y1), (x0, y1))
        rotated = [self._rotate_oriented_point(x, y, oriented_w, oriented_h) for x, y in corners]
        min_x = min(x for x, _ in rotated)
        max_x = max(x for x, _ in rotated)
        min_y = min(y for _, y in rotated)
        max_y = max(y for _, y in rotated)

        px0 = max(0, math.floor(min_x))
        py0 = max(0, math.floor(min_y))
        px1 = min(self.raw_pixel_width, math.ceil(max_x))
        py1 = min(self.raw_pixel_height, math.ceil(max_y))
        if px1 <= px0 or py1 <= py0:
            return None
        return Rect(px0, py0, px1 - px0, py1 - py0)

    def _rotate_oriented_point(
        self, x: float, y: float, width: float, height: float
    ) -> tuple[float, float]:
        """Rotate an oriented pixel-boundary point clockwise into raw space."""

        if self.rotation == 0:
            return x, y
        if self.rotation == 90:
            return height - y, x
        if self.rotation == 180:
            return width - x, height - y
        # 270 clockwise is equivalent to 90 counter-clockwise.
        return y, width - x

    def clip_pixel_rect(self, rect: Rect) -> Rect | None:
        clipped = intersection(rect, self.pixel_bounds)
        if clipped is None:
            return None
        return Rect(
            int(math.floor(clipped.x)),
            int(math.floor(clipped.y)),
            int(math.ceil(clipped.right)) - int(math.floor(clipped.x)),
            int(math.ceil(clipped.bottom)) - int(math.floor(clipped.y)),
        )

    def assert_frame_size(self, width: int, height: int) -> None:
        if (width, height) != (self.raw_pixel_width, self.raw_pixel_height):
            raise FrameDimensionError(
                f"display {self.id!r} expects "
                f"{self.raw_pixel_width}x{self.raw_pixel_height} frame, "
                f"got {width}x{height}"
            )


@dataclass(frozen=True, slots=True, init=False)
class DisplayTopology:
    """An immutable named display arrangement."""

    version: int
    displays: tuple[Display, ...]

    def __init__(
        self,
        version: int,
        displays: Sequence[Display] = (),
    ):
        object.__setattr__(self, "version", version)
        object.__setattr__(self, "displays", tuple(displays))
        ids = [display.id for display in self.displays]
        if len(ids) != len(set(ids)):
            raise ValueError("display ids in a topology must be unique")

    def by_id(self) -> Mapping[str, Display]:
        return {display.id: display for display in self.displays}

    def get(self, display_id: str) -> Display:
        for display in self.displays:
            if display.id == display_id:
                return display
        raise KeyError(f"display {display_id!r} is not in topology v{self.version}")
