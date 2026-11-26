"""Local synthetic demonstration with small pixel grids.

Run with::

    python3 -m frame_occlusion.demo

Characters are synthetic pixels:

* ``.`` / ``N`` / ``O``: ordinary captured pixels
* ``P``: synthetic protected-window pixels in the unverified input
* ``#``: opaque occlusion mask applied before publication
* ``?``: display-only marker for a region whose publication is paused

``?`` cells are absent from the emitted patches; they are only drawn by the
preview renderer.
"""

from __future__ import annotations

import io
from contextlib import redirect_stdout

from .displays import Display, DisplayTopology
from .engine import FramePacket, OcclusionEngine, release_withheld_patches
from .geometry import Rect
from .pixels import PixelGrid
from .windows import Window, WindowSnapshot


def _grid_block(title: str, rows: list[str]) -> list[str]:
    return [title, *rows, ""]


def _horizontal_join(left: list[str], right: list[str], separator: str = "|") -> list[str]:
    if len(left) != len(right):
        raise ValueError("side-by-side grids must have the same row count")
    return [a + separator + b for a, b in zip(left, right)]


def cross_screen_opaque_cover_demo() -> list[str]:
    topology = DisplayTopology(
        version=7,
        displays=[
            Display("left", Rect(0, 0, 5, 3)),
            # Right display is rotated 90 degrees clockwise into the raw buffer.
            Display("right", Rect(5, 0, 5, 3), rotation=90),
        ],
    )
    windows = WindowSnapshot(
        version=1,
        windows=[
            Window("secret", Rect(4, 1, 3, 1), z_index=10, protected=True),
            Window("normal-opaque-cover", Rect(6, 1, 1, 1), z_index=20),
        ],
    )
    raw_left = PixelGrid.from_rows([".....", "....P", "....."])
    raw_right = PixelGrid.from_rows([".P.", ".O.", "...", "...", "..."])

    engine = OcclusionEngine()
    engine.submit_topology(topology)
    engine.submit_windows(windows)
    result = engine.process_frame(
        FramePacket(
            "cross-ready",
            topology_version=7,
            window_version=1,
            images={"left": raw_left, "right": raw_right},
        )
    )

    lines = ["1. Cross-screen window + opaque normal cover", ""]
    lines += _grid_block("left raw:", raw_left.rows())
    lines += _grid_block("left published:", result.preview_rows("left"))
    lines += _grid_block("right raw (rotated raw buffer):", raw_right.rows())
    lines += _grid_block("right published:", result.preview_rows("right"))
    lines.append(
        "Occlusion pixel rects: "
        + ", ".join(
            f"{display_id}={result.display(display_id).occluded_rects}"
            for display_id in ("left", "right")
        )
    )
    lines.append("The right-display 'O' pixel is retained because that part of")
    lines.append("the protected window is fully covered by an opaque normal window.")
    lines.append("")
    return lines


def delayed_drag_demo() -> list[str]:
    topology = DisplayTopology(
        version=1,
        displays=[
            Display("left", Rect(0, 0, 4, 3)),
            Display("right", Rect(4, 0, 4, 3)),
        ],
    )
    windows_v1 = WindowSnapshot(
        version=1,
        windows=[
            Window("static-secret", Rect(1, 0, 1, 1), z_index=10, protected=True),
            Window("dragged-secret", Rect(2, 1, 1, 1), z_index=11, protected=True),
        ],
    )
    windows_v2 = WindowSnapshot(
        version=2,
        windows=[
            Window("static-secret", Rect(1, 0, 1, 1), z_index=10, protected=True),
            # Drag has crossed from left x=2 to right-local x=0..2.
            Window("dragged-secret", Rect(4, 1, 2, 1), z_index=11, protected=True),
        ],
    )

    frame = FramePacket(
        "drag-frame-v2-early",
        topology_version=1,
        window_version=2,
        images={
            "left": PixelGrid.from_rows([".P..", "....", "N..."]),
            "right": PixelGrid.from_rows(["...N", "PP..", "..N."]),
        },
        # Conservative capture/drag damage: old position x=2 through new x=6.
        affected_logical_rects=[Rect(2, 1, 4, 1)],
    )

    engine = OcclusionEngine()
    engine.submit_topology(topology)
    engine.submit_windows(windows_v1)
    pending = engine.process_frame(frame)

    lines = ["2. Drag frame v2 arrives before window state v2", ""]
    lines += _grid_block("left immediate preview (?=paused):", pending.preview_rows("left"))
    lines += _grid_block(
        "right immediate preview (?=paused):", pending.preview_rows("right")
    )
    lines.append("Combined desktop, '|' is the monitor boundary:")
    lines.extend(
        "  " + row
        for row in _horizontal_join(
            pending.preview_rows("left"), pending.preview_rows("right")
        )
    )
    lines.append("")
    patch_rects = [
        f"{patch.display_id}:{tuple(patch.rect)}" for patch in pending.patches
    ]
    publishable_cells = sum(
        display.safe_grid.published_cell_count()
        for display in pending.displays.values()
    )
    lines.append(f"Immediate publishable patches: {patch_rects}")
    lines.append(f"Publishable cells: {publishable_cells}/24; paused cells: 4/24.")
    lines.append("Static protection and ordinary N pixels continue to be published.")
    lines.append("")

    engine.submit_windows(windows_v2)
    ready = engine.process_frame(frame)
    lines.append("After window state v2 arrives, reprocessing the same frame:")
    lines += _grid_block("left complete:", ready.preview_rows("left"))
    lines += _grid_block("right complete:", ready.preview_rows("right"))
    lines.append("Combined complete desktop:")
    lines.extend(
        "  " + row
        for row in _horizontal_join(
            ready.preview_rows("left"), ready.preview_rows("right")
        )
    )
    lines.append("")

    release = release_withheld_patches(ready, pending)
    lines.append("Only these previously withheld patches now need to be sent:")
    for patch in release:
        lines.append(
            f"- {patch.display_id} {tuple(patch.rect)}: {patch.image.rows()}"
        )
    lines.append("")
    return lines


def scaling_boundary_demo() -> list[str]:
    display = Display("scaled", Rect(0, 0, 4, 3), scale=2)
    rect = Rect(1, 1, 2, 1)
    mapped = display.logical_to_pixel_rect(rect)
    return [
        "3. Scaling transform",
        f"Display bounds={display.bounds}, scale=2 -> "
        f"{display.raw_pixel_width}x{display.raw_pixel_height} pixels",
        f"Global logical rect {rect} -> raw pixel rect {mapped}",
        "",
    ]


def build_demo_output() -> str:
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        main()
    return buffer.getvalue()


def main() -> None:
    print("Frame occlusion synthetic demo")
    print("=" * 32)
    print()
    for line in cross_screen_opaque_cover_demo():
        print(line)
    for line in delayed_drag_demo():
        print(line)
    for line in scaling_boundary_demo():
        print(line)


if __name__ == "__main__":
    main()
