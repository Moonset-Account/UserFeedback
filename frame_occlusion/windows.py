"""Window geometry, opacity, stacking order and versioned snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .geometry import Rect


@dataclass(frozen=True, slots=True)
class Window:
    """A window in global logical desktop coordinates.

    Larger ``z_index`` means closer to the viewer/above smaller values.  Ties
    preserve the order supplied to :class:`WindowSnapshot`, which is stable but
    should be avoided by producers.
    """

    id: str
    rect: Rect
    z_index: int = 0
    protected: bool = False
    visible: bool = True
    opacity: float = 1.0
    opaque: bool | None = None

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("window id must not be empty")
        if not 0.0 <= self.opacity <= 1.0:
            raise ValueError("opacity must be in [0, 1]")

    @property
    def has_actual_pixels(self) -> bool:
        return self.visible and not self.rect.is_empty and self.opacity > 0.0

    @property
    def is_fully_opaque(self) -> bool:
        """Whether the whole rectangle can hide windows below it."""

        return (
            self.visible
            and self.opacity >= 1.0
            and self.opaque is not False
            and not self.rect.is_empty
        )


@dataclass(frozen=True, init=False)
class WindowSnapshot:
    """An immutable full window state at one version."""

    version: int
    windows: tuple[Window, ...]

    def __init__(self, version: int, windows: Sequence[Window] = ()):
        object.__setattr__(self, "version", version)
        object.__setattr__(self, "windows", tuple(windows))
        ids = [window.id for window in self.windows]
        if len(ids) != len(set(ids)):
            raise ValueError("window ids in a snapshot must be unique")

    def ordered_bottom_to_top(self) -> list[Window]:
        # Python's sort is stable, so equal z values retain producer order.
        indexed = sorted(
            enumerate(self.windows), key=lambda item: (item[1].z_index, item[0])
        )
        return [window for _, window in indexed]

    def get(self, window_id: str) -> Window:
        for window in self.windows:
            if window.id == window_id:
                return window
        raise KeyError(window_id)
