import unittest

from frame_occlusion import (
    Display,
    DisplayTopology,
    FramePacket,
    OcclusionEngine,
    PixelGrid,
    Rect,
    Window,
    WindowSnapshot,
    compute_display_occlusion_rects,
)


class VisiblePixelOcclusionTests(unittest.TestCase):
    def setUp(self):
        self.topology = DisplayTopology(
            7,
            [
                Display("L", Rect(0, 0, 5, 3)),
                Display("R", Rect(5, 0, 5, 3), rotation=90),
            ],
        )
        self.snapshot = WindowSnapshot(
            1,
            [
                Window("secret", Rect(4, 1, 3, 1), z_index=10, protected=True),
                # Covers the right-screen half of the crossing protected window.
                Window("opaque-cover", Rect(6, 1, 1, 1), z_index=20, opaque=True),
            ],
        )

    def test_cross_screen_rectangles_clip_and_rotate_into_pixels(self):
        left_rects = compute_display_occlusion_rects(
            self.topology, self.snapshot, "L"
        )
        right_rects = compute_display_occlusion_rects(
            self.topology, self.snapshot, "R"
        )

        self.assertEqual(left_rects, [Rect(4, 1, 1, 1)])
        # The opaque window removes raw cell (1, 1); only (1, 0) remains exposed.
        self.assertEqual(right_rects, [Rect(1, 0, 1, 1)])

    def test_output_masks_visible_secret_but_keeps_normal_opaque_pixels(self):
        left = PixelGrid.from_rows(
            [
                ".....",
                "....P",
                ".....",
            ]
        )
        right = PixelGrid.from_rows(
            [
                ".P.",
                ".O.",
                "...",
                "...",
                "...",
            ]
        )
        engine = OcclusionEngine()
        engine.submit_topology(self.topology)
        engine.submit_windows(self.snapshot)

        result = engine.process_frame(
            FramePacket(
                "f1",
                topology_version=7,
                window_version=1,
                images={"L": left, "R": right},
            )
        )

        self.assertTrue(result.is_complete)
        self.assertEqual(
            result.preview_rows("L"),
            [
                ".....",
                "....#",
                ".....",
            ],
        )
        self.assertEqual(
            result.preview_rows("R"),
            [
                ".#.",
                ".O.",  # normal opaque-window pixel is retained
                "...",
                "...",
                "...",
            ],
        )
        # Input grids must not be mutated.
        self.assertEqual(left.get(4, 1), "P")

    def test_translucent_window_does_not_count_as_full_cover(self):
        topology = DisplayTopology(1, [Display("d", Rect(0, 0, 4, 3))])
        snapshot = WindowSnapshot(
            1,
            [
                Window("secret", Rect(0, 0, 2, 2), z_index=1, protected=True),
                Window(
                    "glass",
                    Rect(1, 1, 2, 2),
                    z_index=2,
                    opacity=0.5,
                ),
            ],
        )
        rects = compute_display_occlusion_rects(topology, snapshot, "d")
        self.assertEqual(rects, [Rect(0, 0, 2, 2)])

    def test_explicit_non_opaque_window_does_not_cover_even_at_alpha_one(self):
        topology = DisplayTopology(1, [Display("d", Rect(0, 0, 4, 3))])
        snapshot = WindowSnapshot(
            1,
            [
                Window("secret", Rect(0, 0, 2, 2), z_index=1, protected=True),
                Window(
                    "claimed-transparent",
                    Rect(1, 1, 2, 2),
                    z_index=2,
                    opacity=1.0,
                    opaque=False,
                ),
            ],
        )
        rects = compute_display_occlusion_rects(topology, snapshot, "d")
        self.assertEqual(rects, [Rect(0, 0, 2, 2)])

    def test_lower_z_window_cannot_cover_protected_window(self):
        topology = DisplayTopology(1, [Display("d", Rect(0, 0, 4, 3))])
        snapshot = WindowSnapshot(
            1,
            [
                Window("normal-below", Rect(1, 1, 2, 2), z_index=0),
                Window("secret", Rect(0, 0, 2, 2), z_index=1, protected=True),
            ],
        )
        rects = compute_display_occlusion_rects(topology, snapshot, "d")
        self.assertEqual(rects, [Rect(0, 0, 2, 2)])

    def test_protected_opaque_above_still_masks_shared_area(self):
        image = PixelGrid.from_rows(["....", "....", "....", "...."])
        topology = DisplayTopology(1, [Display("d", Rect(0, 0, 4, 4))])
        snapshot = WindowSnapshot(
            1,
            [
                Window("lower-secret", Rect(0, 0, 2, 2), z_index=1, protected=True),
                Window("upper-secret", Rect(1, 1, 2, 2), z_index=2, protected=True),
            ],
        )
        engine = OcclusionEngine()
        engine.submit_topology(topology)
        engine.submit_windows(snapshot)
        result = engine.process_frame(FramePacket.single_display("f", "d", image, 1, 1))

        # Union of lower visible pieces and upper mask covers every original pixel.
        for y in range(2):
            for x in range(2):
                self.assertEqual(result.display("d").safe_grid.get(x, y), "#")
        for y in range(1, 3):
            for x in range(1, 3):
                self.assertEqual(result.display("d").safe_grid.get(x, y), "#")


if __name__ == "__main__":
    unittest.main()
