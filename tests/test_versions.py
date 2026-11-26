import unittest

from frame_occlusion import (
    TOPOLOGY_PENDING,
    WINDOW_PENDING,
    Display,
    DisplayTopology,
    FrameDimensionError,
    FramePacket,
    OcclusionEngine,
    PixelGrid,
    Rect,
    Window,
    WindowSnapshot,
    WindowVersionUnavailable,
    release_withheld_patches,
)


def drag_topology() -> DisplayTopology:
    return DisplayTopology(
        1,
        [
            Display("L", Rect(0, 0, 4, 3)),
            Display("R", Rect(4, 0, 4, 3)),
        ],
    )


def baseline_windows() -> WindowSnapshot:
    return WindowSnapshot(
        1,
        [
            Window("static-secret", Rect(1, 0, 1, 1), z_index=10, protected=True),
            Window("dragged-secret", Rect(2, 1, 1, 1), z_index=11, protected=True),
        ],
    )


def drag_frame() -> FramePacket:
    # The captured frame already contains the dragged pixels on R, but window
    # state v2 has not arrived.  The damage/drag bounds cover old x=2 and new
    # x=4..6 positions.
    left = PixelGrid.from_rows(
        [
            ".P..",  # static protected window
            "....",  # old drag location is already repainted in frame v2
            "....",
        ]
    )
    right = PixelGrid.from_rows(
        [
            "....",
            "PP..",  # new dragged location visible in the early frame
            "....",
        ]
    )
    return FramePacket(
        "drag-frame-2",
        topology_version=1,
        window_version=2,
        images={"L": left, "R": right},
        affected_logical_rects=[Rect(2, 1, 4, 1)],
    )


class VersionInterleavingTests(unittest.TestCase):
    def test_early_drag_frame_pauses_only_affected_regions(self):
        engine = OcclusionEngine()
        engine.submit_topology(drag_topology())
        engine.submit_windows(baseline_windows())

        result = engine.process_frame(drag_frame())

        self.assertEqual(result.status, WINDOW_PENDING)
        self.assertTrue(result.has_paused_regions)
        self.assertEqual(result.window_snapshot_used, 1)

        left = result.display("L")
        right = result.display("R")
        self.assertEqual(left.paused_rects, [Rect(2, 1, 2, 1)])
        self.assertEqual(right.paused_rects, [Rect(0, 1, 2, 1)])

        self.assertEqual(
            left.preview_rows(),
            [
                ".#..",  # static protected window is still masked elsewhere
                "..??",  # affected old/new drag strip is withheld (cols 2-3)
                "....",
            ],
        )
        self.assertEqual(
            right.preview_rows(),
            [
                "....",
                "??..",  # raw "PP" must not be published
                "....",
            ],
        )

        # Every emitted patch is fully outside paused rectangles and has no holes.
        for patch in result.patches:
            rows = patch.image.rows()  # raises if an unpublished None cell escaped
            self.assertNotIn("P", "".join(rows))
            for paused in result.display(patch.display_id).paused_rects:
                from frame_occlusion.geometry import intersection

                self.assertIsNone(intersection(patch.rect, paused))

        for display_result in result.displays.values():
            for y in range(display_result.height):
                for x in range(display_result.width):
                    value = display_result.safe_grid.get(x, y)
                    if value is not None:
                        self.assertNotEqual(value, "P")

        # 4*3 - 2 paused cells on each display.
        self.assertEqual(left.safe_grid.published_cell_count(), 10)
        self.assertEqual(right.safe_grid.published_cell_count(), 10)
        self.assertEqual(len(left.patches), 3)
        self.assertEqual(len(right.patches), 3)

        # The static mask is present in an immediately publishable top patch.
        top_left = next(p for p in left.patches if p.rect == Rect(0, 0, 4, 1))
        self.assertEqual(top_left.image.rows(), [".#.."])

    def test_when_window_state_arrives_same_frame_can_be_reprocessed(self):
        engine = OcclusionEngine()
        engine.submit_topology(drag_topology())
        engine.submit_windows(baseline_windows())
        frame = drag_frame()

        pending = engine.process_frame(frame)
        self.assertEqual(pending.status, WINDOW_PENDING)

        engine.submit_windows(
            WindowSnapshot(
                2,
                [
                    Window("static-secret", Rect(1, 0, 1, 1), 10, protected=True),
                    Window("dragged-secret", Rect(4, 1, 2, 1), 11, protected=True),
                ],
            )
        )
        ready = engine.process_frame(frame)

        self.assertTrue(ready.is_complete)
        self.assertFalse(ready.has_paused_regions)
        self.assertEqual(
            ready.preview_rows("L"),
            [
                ".#..",
                "....",
                "....",
            ],
        )
        self.assertEqual(
            ready.preview_rows("R"),
            [
                "....",
                "##..",
                "....",
            ],
        )
        self.assertEqual(len(ready.patches), 2)

        release = release_withheld_patches(ready, pending)
        self.assertEqual(
            [(patch.display_id, patch.rect) for patch in release],
            [
                ("L", Rect(2, 1, 2, 1)),
                ("R", Rect(0, 1, 2, 1)),
            ],
        )
        release_by_display = {patch.display_id: patch for patch in release}
        self.assertEqual(release_by_display["L"].image.rows(), [".."])
        self.assertEqual(release_by_display["R"].image.rows(), ["##"])

    def test_release_helper_rejects_incomplete_or_mismatched_results(self):
        engine = OcclusionEngine()
        engine.submit_topology(drag_topology())
        engine.submit_windows(baseline_windows())
        pending = engine.process_frame(drag_frame())

        with self.assertRaises(ValueError):
            release_withheld_patches(pending, pending)

        other = FramePacket(
            "other-id",
            1,
            1,
            {"L": PixelGrid(4, 3), "R": PixelGrid(4, 3)},
        )
        ready_other = engine.process_frame(other)
        self.assertTrue(ready_other.is_complete)
        with self.assertRaises(ValueError):
            release_withheld_patches(ready_other, pending)

    def test_missing_affected_bounds_fails_closed_for_whole_display(self):
        engine = OcclusionEngine()
        engine.submit_topology(drag_topology())
        engine.submit_windows(baseline_windows())
        frame = FramePacket(
            "no-damage",
            1,
            2,
            {"L": PixelGrid(4, 3), "R": PixelGrid(4, 3)},
        )

        result = engine.process_frame(frame)
        self.assertEqual(result.status, WINDOW_PENDING)
        for display_id in ("L", "R"):
            display_result = result.display(display_id)
            self.assertEqual(display_result.paused_rects, [Rect(0, 0, 4, 3)])
            self.assertEqual(display_result.patches, [])
            self.assertEqual(display_result.safe_grid.published_cell_count(), 0)

    def test_explicit_empty_affected_region_allows_other_patches(self):
        engine = OcclusionEngine()
        engine.submit_topology(drag_topology())
        engine.submit_windows(baseline_windows())
        frame = FramePacket(
            "explicit-empty",
            1,
            2,
            {"L": PixelGrid(4, 3, "."), "R": PixelGrid(4, 3, ".")},
            affected_logical_rects=[],
        )

        result = engine.process_frame(frame)
        self.assertEqual(result.status, WINDOW_PENDING)
        self.assertFalse(result.has_paused_regions)
        self.assertEqual(len(result.patches), 2)

    def test_pixel_damage_rects_are_clipped(self):
        engine = OcclusionEngine()
        topology = DisplayTopology(1, [Display("d", Rect(0, 0, 4, 3))])
        engine.submit_topology(topology)
        engine.submit_windows(baseline_windows())  # v1, frame asks v2
        frame = FramePacket(
            "pixel-damage",
            1,
            2,
            {"d": PixelGrid(4, 3, ".")},
            affected_logical_rects=[],
            affected_pixel_rects={"d": [Rect(2, 1, 5, 1)]},
        )
        result = engine.process_frame(frame)
        self.assertEqual(result.display("d").paused_rects, [Rect(2, 1, 2, 1)])

    def test_topology_and_window_messages_can_arrive_after_frame(self):
        engine = OcclusionEngine()
        frame = drag_frame()

        first = engine.process_frame(frame)
        self.assertEqual(first.status, TOPOLOGY_PENDING)
        self.assertEqual(first.display("L").patches, [])
        self.assertEqual(first.display("R").patches, [])

        engine.submit_topology(drag_topology())
        second = engine.process_frame(frame)
        self.assertEqual(second.status, WINDOW_PENDING)
        self.assertIn("no older baseline", second.reason or "")
        self.assertEqual(second.patches, [])

        engine.submit_windows(baseline_windows())
        third = engine.process_frame(frame)
        self.assertEqual(third.status, WINDOW_PENDING)
        self.assertEqual(len(third.patches), 6)

        engine.submit_windows(
            WindowSnapshot(
                2,
                [
                    Window("static-secret", Rect(1, 0, 1, 1), 10, protected=True),
                    Window("dragged-secret", Rect(4, 1, 2, 1), 11, protected=True),
                ],
            )
        )
        fourth = engine.process_frame(frame)
        self.assertTrue(fourth.is_complete)
        self.assertEqual(len(fourth.patches), 2)

    def test_custom_hold_character_flows_to_preview(self):
        engine = OcclusionEngine(hold_char="X")
        engine.submit_topology(DisplayTopology(1, [Display("d", Rect(0, 0, 2, 2))]))
        result = engine.process_frame(
            FramePacket("f", 1, 2, {"d": PixelGrid(2, 2, ".")})
        )
        self.assertEqual(result.preview_rows("d"), ["XX", "XX"])
        self.assertEqual(result.preview_rows("d", "?"), ["??", "??"])

    def test_unknown_display_or_wrong_frame_size_is_rejected(self):
        engine = OcclusionEngine()
        engine.submit_topology(drag_topology())
        engine.submit_windows(baseline_windows())

        with self.assertRaises(KeyError):
            engine.process_frame(
                FramePacket(
                    "unknown-display",
                    1,
                    1,
                    {"missing": PixelGrid(4, 3)},
                )
            )

        with self.assertRaises(FrameDimensionError):
            engine.process_frame(
                FramePacket(
                    "bad-size",
                    1,
                    1,
                    {"L": PixelGrid(3, 3)},
                )
            )

    def test_newer_snapshot_is_not_used_as_implicit_older_baseline(self):
        engine = OcclusionEngine()
        engine.submit_topology(drag_topology())
        engine.submit_windows(
            WindowSnapshot(
                3,
                [Window("different", Rect(0, 0, 1, 1), protected=True)],
            )
        )
        frame = drag_frame()  # asks v2

        result = engine.process_frame(frame)
        self.assertEqual(result.status, WINDOW_PENDING)
        self.assertIsNone(result.window_snapshot_used)
        self.assertEqual(result.patches, [])

        explicit = engine.process_frame(
            FramePacket(
                frame.frame_id,
                frame.topology_version,
                frame.window_version,
                frame.images,
                affected_logical_rects=frame.affected_logical_rects,
                fallback_window_version=3,
            )
        )
        self.assertEqual(explicit.window_snapshot_used, 3)
        self.assertTrue(explicit.has_paused_regions)

        with self.assertRaises(WindowVersionUnavailable):
            engine.process_frame(
                FramePacket(
                    "bad-fallback",
                    1,
                    2,
                    {"L": PixelGrid(4, 3)},
                    fallback_window_version=99,
                )
            )


if __name__ == "__main__":
    unittest.main()
