"""Version-aware per-frame protected-window occlusion.

The public API is intentionally dependency-free.  Start with
:class:`frame_occlusion.OcclusionEngine`, submit immutable topology/window
versions, then pass :class:`frame_occlusion.FramePacket` frames to
``process_frame``.
"""

from .displays import (
    Display,
    DisplayTopology,
    FrameDimensionError,
    TopologyVersionUnavailable,
)
from .engine import (
    COMPLETE,
    TOPOLOGY_PENDING,
    WINDOW_PENDING,
    DisplayResult,
    FramePacket,
    FrameResult,
    OcclusionEngine,
    PixelPatch,
    WindowVersionUnavailable,
    release_withheld_patches,
)
from .geometry import Rect
from .occlusion import (
    compute_display_occlusion_rects,
    occlude_display_grid,
    protected_visible_logical_regions,
)
from .pixels import PixelGrid
from .windows import Window, WindowSnapshot

__all__ = [
    "COMPLETE",
    "TOPOLOGY_PENDING",
    "WINDOW_PENDING",
    "Display",
    "DisplayResult",
    "DisplayTopology",
    "FrameDimensionError",
    "FramePacket",
    "FrameResult",
    "OcclusionEngine",
    "PixelGrid",
    "PixelPatch",
    "Rect",
    "TopologyVersionUnavailable",
    "Window",
    "WindowSnapshot",
    "WindowVersionUnavailable",
    "compute_display_occlusion_rects",
    "occlude_display_grid",
    "protected_visible_logical_regions",
    "release_withheld_patches",
]

__version__ = "1.0.0"
