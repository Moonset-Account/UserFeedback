"""Version-aware per-frame occlusion and region publication."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence

from .displays import Display, DisplayTopology
from .geometry import Rect, full_pixel_rect, subtract_all, subtract_many
from .occlusion import compute_display_occlusion_rects
from .pixels import PixelGrid
from .windows import WindowSnapshot

COMPLETE = "complete"
TOPOLOGY_PENDING = "topology_version_pending"
WINDOW_PENDING = "window_version_pending"


class WindowVersionUnavailable(KeyError):
    """An explicitly selected fallback window snapshot is not stored."""


@dataclass(frozen=True, slots=True)
class PixelPatch:
    """A publishable rectangular sub-region of one display frame."""

    display_id: str
    rect: Rect
    image: PixelGrid


@dataclass
class DisplayResult:
    display_id: str
    width: int
    height: int
    safe_grid: PixelGrid
    occluded_rects: list[Rect] = field(default_factory=list)
    paused_rects: list[Rect] = field(default_factory=list)
    published_rects: list[Rect] = field(default_factory=list)
    patches: list[PixelPatch] = field(default_factory=list)
    baseline_window_version: int | None = None
    hold_char: str = "?"

    @property
    def has_paused_regions(self) -> bool:
        return bool(self.paused_rects)

    def preview_rows(self, hold_char: str | None = None) -> list[str]:
        """Render masked cells and display-only markers for paused cells."""

        return self.safe_grid.render(hold_char or self.hold_char)


@dataclass
class FrameResult:
    frame_id: str
    status: str
    topology_version: int
    window_version: int
    displays: dict[str, DisplayResult]
    window_snapshot_used: int | None = None
    reason: str | None = None

    @property
    def is_complete(self) -> bool:
        return self.status == COMPLETE

    @property
    def has_paused_regions(self) -> bool:
        return any(display.has_paused_regions for display in self.displays.values())

    @property
    def patches(self) -> list[PixelPatch]:
        return [
            patch
            for display in self.displays.values()
            for patch in display.patches
        ]

    def display(self, display_id: str) -> DisplayResult:
        return self.displays[display_id]

    def preview_rows(self, display_id: str, hold_char: str | None = None) -> list[str]:
        display_result = self.displays[display_id]
        if hold_char is None:
            return display_result.preview_rows()
        return display_result.preview_rows(hold_char)


@dataclass(frozen=True)
class FramePacket:
    """One captured frame or synchronized group of display frames.

    ``affected_logical_rects`` / ``affected_pixel_rects`` are conservative
    regions for which occlusion classification may not yet be valid because the
    referenced window snapshot has not arrived.  They commonly come from capture
    damage/drag bounds.  When both are ``None`` and the exact window snapshot is
    unavailable, the whole frame is paused (fail closed).
    """

    frame_id: str
    topology_version: int
    window_version: int
    images: Mapping[str, PixelGrid]
    affected_logical_rects: Sequence[Rect] | None = None
    affected_pixel_rects: Mapping[str, Sequence[Rect]] | None = None
    fallback_window_version: int | None = None

    @classmethod
    def single_display(
        cls,
        frame_id: str,
        display_id: str,
        image: PixelGrid,
        topology_version: int,
        window_version: int,
        **kwargs: object,
    ) -> "FramePacket":
        return cls(
            frame_id=frame_id,
            topology_version=topology_version,
            window_version=window_version,
            images={display_id: image},
            **kwargs,  # type: ignore[arg-type]
        )


class OcclusionEngine:
    """Store versioned topology/window states and process captured frames."""

    def __init__(self, mask_char: str = "#", hold_char: str = "?") -> None:
        if len(mask_char) != 1 or len(hold_char) != 1:
            raise ValueError("mask_char and hold_char must be one character")
        self.mask_char = mask_char
        self.hold_char = hold_char
        self._topologies: dict[int, DisplayTopology] = {}
        self._windows: dict[int, WindowSnapshot] = {}

    def submit_topology(self, topology: DisplayTopology) -> None:
        self._topologies[topology.version] = topology

    def submit_windows(self, snapshot: WindowSnapshot) -> None:
        self._windows[snapshot.version] = snapshot

    # Convenience aliases for event-stream wording.
    update_topology = submit_topology
    update_windows = submit_windows

    def get_topology(self, version: int) -> DisplayTopology | None:
        return self._topologies.get(version)

    def get_windows(self, version: int) -> WindowSnapshot | None:
        return self._windows.get(version)

    def process_frame(self, frame: FramePacket) -> FrameResult:
        for display_id, image in frame.images.items():
            if not isinstance(image, PixelGrid):
                raise TypeError(f"image for {display_id!r} must be a PixelGrid")

        topology = self._topologies.get(frame.topology_version)
        if topology is None:
            return self._topology_pending(frame)

        # Validate all known displays before publishing anything.
        displays_by_id = topology.by_id()
        for display_id, image in frame.images.items():
            if display_id not in displays_by_id:
                raise KeyError(
                    f"frame contains display {display_id!r} absent from "
                    f"topology v{frame.topology_version}"
                )
            displays_by_id[display_id].assert_frame_size(image.width, image.height)

        exact_snapshot = self._windows.get(frame.window_version)
        if exact_snapshot is not None:
            return self._process_ready(frame, topology, exact_snapshot)

        baseline = self._select_baseline(frame)
        if baseline is None:
            return self._window_pending_without_baseline(frame)

        return self._process_with_baseline(frame, topology, baseline)

    def _process_ready(
        self,
        frame: FramePacket,
        topology: DisplayTopology,
        snapshot: WindowSnapshot,
    ) -> FrameResult:
        results: dict[str, DisplayResult] = {}
        for display_id, image in frame.images.items():
            display = topology.get(display_id)
            safe_grid = image.copy()
            rects = compute_display_occlusion_rects(topology, snapshot, display_id)
            for rect in rects:
                safe_grid.fill_rect(rect, self.mask_char)

            full = full_pixel_rect(image.width, image.height)
            patch = PixelPatch(display_id, full, safe_grid.extract(full))
            results[display_id] = DisplayResult(
                display_id=display_id,
                width=image.width,
                height=image.height,
                safe_grid=safe_grid,
                occluded_rects=rects,
                published_rects=[full],
                patches=[patch],
                baseline_window_version=snapshot.version,
                hold_char=self.hold_char,
            )

        return FrameResult(
            frame_id=frame.frame_id,
            status=COMPLETE,
            topology_version=frame.topology_version,
            window_version=frame.window_version,
            displays=results,
            window_snapshot_used=snapshot.version,
        )

    def _process_with_baseline(
        self,
        frame: FramePacket,
        topology: DisplayTopology,
        baseline: WindowSnapshot,
    ) -> FrameResult:
        results: dict[str, DisplayResult] = {}

        unspecified_affected = (
            frame.affected_logical_rects is None
            and frame.affected_pixel_rects is None
        )

        for display_id, image in frame.images.items():
            display = topology.get(display_id)
            safe_grid = image.copy()
            occluded = compute_display_occlusion_rects(topology, baseline, display_id)
            for rect in occluded:
                safe_grid.fill_rect(rect, self.mask_char)

            if unspecified_affected:
                paused = [full_pixel_rect(image.width, image.height)]
            else:
                paused = self._collect_paused_rects(frame, display)

            full = full_pixel_rect(image.width, image.height)
            published = [full]
            for paused_rect in paused:
                published = subtract_many(published, paused_rect)
                safe_grid.clear_rect(paused_rect)

            patches = [
                PixelPatch(display_id, rect, safe_grid.extract(rect))
                for rect in published
                if not rect.is_empty
            ]
            results[display_id] = DisplayResult(
                display_id=display_id,
                width=image.width,
                height=image.height,
                safe_grid=safe_grid,
                occluded_rects=occluded,
                paused_rects=paused,
                published_rects=published,
                patches=patches,
                baseline_window_version=baseline.version,
                hold_char=self.hold_char,
            )

        return FrameResult(
            frame_id=frame.frame_id,
            status=WINDOW_PENDING,
            topology_version=frame.topology_version,
            window_version=frame.window_version,
            displays=results,
            window_snapshot_used=baseline.version,
            reason=(
                f"window snapshot v{frame.window_version} has not arrived; "
                f"using v{baseline.version} baseline outside affected regions"
            ),
        )

    def _topology_pending(self, frame: FramePacket) -> FrameResult:
        results: dict[str, DisplayResult] = {}
        for display_id, image in frame.images.items():
            safe_grid = image.copy()
            full = full_pixel_rect(image.width, image.height)
            safe_grid.clear_rect(full)
            results[display_id] = DisplayResult(
                display_id=display_id,
                width=image.width,
                height=image.height,
                safe_grid=safe_grid,
                paused_rects=[full],
                published_rects=[],
                patches=[],
                hold_char=self.hold_char,
            )
        return FrameResult(
            frame_id=frame.frame_id,
            status=TOPOLOGY_PENDING,
            topology_version=frame.topology_version,
            window_version=frame.window_version,
            displays=results,
            reason=f"topology v{frame.topology_version} has not arrived",
        )

    def _window_pending_without_baseline(self, frame: FramePacket) -> FrameResult:
        results: dict[str, DisplayResult] = {}
        for display_id, image in frame.images.items():
            safe_grid = image.copy()
            full = full_pixel_rect(image.width, image.height)
            safe_grid.clear_rect(full)
            results[display_id] = DisplayResult(
                display_id=display_id,
                width=image.width,
                height=image.height,
                safe_grid=safe_grid,
                paused_rects=[full],
                published_rects=[],
                patches=[],
                hold_char=self.hold_char,
            )
        return FrameResult(
            frame_id=frame.frame_id,
            status=WINDOW_PENDING,
            topology_version=frame.topology_version,
            window_version=frame.window_version,
            displays=results,
            reason=(
                f"window snapshot v{frame.window_version} has not arrived and "
                "no older baseline is available; failing closed"
            ),
        )

    def _select_baseline(self, frame: FramePacket) -> WindowSnapshot | None:
        if frame.fallback_window_version is not None:
            try:
                return self._windows[frame.fallback_window_version]
            except KeyError as exc:
                raise WindowVersionUnavailable(
                    f"fallback window v{frame.fallback_window_version} is not stored"
                ) from exc

        older_versions = [
            version
            for version in self._windows
            if version < frame.window_version
        ]
        if not older_versions:
            return None
        return self._windows[max(older_versions)]

    def _collect_paused_rects(self, frame: FramePacket, display: Display) -> list[Rect]:
        paused: list[Rect] = []

        for logical_rect in frame.affected_logical_rects or ():
            pixel_rect = display.logical_to_pixel_rect(logical_rect)
            if pixel_rect is not None:
                paused.append(pixel_rect)

        pixel_rects = (frame.affected_pixel_rects or {}).get(display.id, ())
        for pixel_rect in pixel_rects:
            clipped = display.clip_pixel_rect(pixel_rect)
            if clipped is not None:
                paused.append(clipped)

        return paused


def release_withheld_patches(
    ready_result: FrameResult,
    pending_result: FrameResult,
) -> list[PixelPatch]:
    """Return only regions that were paused and are now safely occluded.

    ``ready_result`` must be the complete result of reprocessing the same frame
    after its exact topology/window versions arrived.  The returned patches are
    suitable for sending after the patches already emitted by
    :meth:`OcclusionEngine.process_frame`.
    """

    if ready_result.frame_id != pending_result.frame_id:
        raise ValueError("ready and pending results must refer to the same frame")
    if not ready_result.is_complete:
        raise ValueError("withheld regions cannot be released before a complete result")

    patches: list[PixelPatch] = []
    for display_id, pending_display in pending_result.displays.items():
        ready_display = ready_result.display(display_id)
        full = full_pixel_rect(pending_display.width, pending_display.height)

        # The complement of the disjoint published region is the withheld area.
        withheld = subtract_all([full], pending_display.published_rects)
        for rect in withheld:
            patches.append(
                PixelPatch(
                    display_id,
                    rect,
                    ready_display.safe_grid.extract(rect),
                )
            )
    return patches
