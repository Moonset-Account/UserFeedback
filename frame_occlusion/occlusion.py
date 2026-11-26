"""Pure occlusion-region calculations."""

from __future__ import annotations

from .displays import Display, DisplayTopology
from .geometry import Rect, subtract_many
from .pixels import PixelGrid
from .windows import WindowSnapshot


def protected_visible_logical_regions(
    snapshot: WindowSnapshot,
) -> dict[str, list[Rect]]:
    """Return visible logical regions for each protected window.

    A protected window's rectangle is reduced by every higher-z fully opaque
    window.  Opaque normal windows therefore leave their own pixels untouched;
    opaque protected windows are still handled by their own later mask.
    """

    ordered = snapshot.ordered_bottom_to_top()
    result: dict[str, list[Rect]] = {}

    for index, window in enumerate(ordered):
        if not window.protected or not window.has_actual_pixels:
            continue

        regions: list[Rect] = [window.rect]
        for above in ordered[index + 1 :]:
            if above.is_fully_opaque:
                regions = subtract_many(regions, above.rect)
                if not regions:
                    break

        if regions:
            result[window.id] = regions

    return result


def compute_display_occlusion_rects(
    topology: DisplayTopology,
    snapshot: WindowSnapshot,
    display_id: str,
) -> list[Rect]:
    """Compute raw pixel rectangles to mask on one display."""

    display = topology.get(display_id)
    logical_regions = protected_visible_logical_regions(snapshot)
    pixel_rects: list[Rect] = []

    for regions in logical_regions.values():
        for region in regions:
            pixel_rect = display.logical_to_pixel_rect(region)
            if pixel_rect is not None:
                pixel_rects.append(pixel_rect)

    return pixel_rects


def occlude_display_grid(
    image: PixelGrid,
    display: Display,
    snapshot: WindowSnapshot,
    mask_char: str = "#",
) -> list[Rect]:
    """Mask a grid in place and return the applied pixel rectangles."""

    rects: list[Rect] = []
    for regions in protected_visible_logical_regions(snapshot).values():
        for region in regions:
            pixel_rect = display.logical_to_pixel_rect(region)
            if pixel_rect is not None:
                image.fill_rect(pixel_rect, mask_char)
                rects.append(pixel_rect)
    return rects
