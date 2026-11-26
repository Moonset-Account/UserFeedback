import unittest

from frame_occlusion import Display, Rect
from frame_occlusion.displays import FrameDimensionError
from frame_occlusion.pixels import PixelGrid


class DisplayTransformTests(unittest.TestCase):
    def test_scale_and_negative_global_origin(self):
        display = Display("d", Rect(-20, 4, 4, 3), scale=2)
        self.assertEqual((display.raw_pixel_width, display.raw_pixel_height), (8, 6))

        # Global (-19, 4)..(-17, 5) -> local (1, 0)..(3, 1), then x2 scale.
        self.assertEqual(
            display.logical_to_pixel_rect(Rect(-19, 4, 2, 1)),
            Rect(2, 0, 4, 2),
        )

    def test_cross_screen_rectangle_is_clipped_to_display_bounds(self):
        left = Display("L", Rect(0, 0, 5, 3))
        right = Display("R", Rect(5, 0, 5, 3))
        crossing = Rect(4, 1, 3, 1)

        self.assertEqual(left.logical_to_pixel_rect(crossing), Rect(4, 1, 1, 1))
        self.assertEqual(right.logical_to_pixel_rect(crossing), Rect(0, 1, 2, 1))

    def test_rectangle_spanning_three_displays_is_clipped_separately(self):
        displays = [
            Display("d0", Rect(0, 0, 4, 2)),
            Display("d1", Rect(4, 0, 4, 2)),
            Display("d2", Rect(8, 0, 4, 2)),
        ]
        spanning = Rect(2, 0, 8, 1)  # x=2..10, crosses all three

        mapped = [display.logical_to_pixel_rect(spanning) for display in displays]
        self.assertEqual(
            mapped,
            [
                Rect(2, 0, 2, 1),
                Rect(0, 0, 4, 1),
                Rect(0, 0, 2, 1),
            ],
        )

    def test_fractional_scale_expands_to_cover_partial_pixels(self):
        display = Display("d", Rect(0, 0, 3, 2), scale=1.5)
        self.assertEqual((display.raw_pixel_width, display.raw_pixel_height), (5, 3))

        # Boundary at x=4.5/y=1.5 must cover through pixels 4 and 1.
        self.assertEqual(
            display.logical_to_pixel_rect(Rect(2, 0, 1, 1)),
            Rect(3, 0, 2, 2),
        )

    def test_rotation_90_swaps_and_moves_mask(self):
        display = Display("R", Rect(5, 0, 5, 3), rotation=90)
        self.assertEqual((display.raw_pixel_width, display.raw_pixel_height), (3, 5))
        self.assertEqual(
            display.logical_to_pixel_rect(Rect(4, 1, 3, 1)),
            Rect(1, 0, 1, 2),
        )

    def test_rotation_180_and_270(self):
        rect = Rect(4, 1, 3, 1)
        d180 = Display("d", Rect(5, 0, 5, 3), rotation=180)
        d270 = Display("d", Rect(5, 0, 5, 3), rotation=270)

        self.assertEqual(d180.logical_to_pixel_rect(rect), Rect(3, 1, 2, 1))
        self.assertEqual(d270.logical_to_pixel_rect(rect), Rect(1, 3, 1, 2))

    def test_combined_scale_and_rotation(self):
        display = Display("d", Rect(0, 0, 3, 2), scale=2, rotation=90)
        self.assertEqual((display.raw_pixel_width, display.raw_pixel_height), (4, 6))
        self.assertEqual(
            display.logical_to_pixel_rect(Rect(1, 0, 2, 1)),
            Rect(2, 2, 2, 4),
        )

    def test_offscreen_and_boundary_only_rects_have_no_pixels(self):
        display = Display("d", Rect(0, 0, 4, 3))
        self.assertIsNone(display.logical_to_pixel_rect(Rect(4, 1, 2, 1)))
        self.assertIsNone(display.logical_to_pixel_rect(Rect(-2, 0, 2, 3)))

    def test_frame_dimension_check(self):
        display = Display("d", Rect(0, 0, 4, 3), scale=2)
        PixelGrid(8, 6)  # expected size
        display.assert_frame_size(8, 6)
        with self.assertRaises(FrameDimensionError):
            display.assert_frame_size(4, 3)


if __name__ == "__main__":
    unittest.main()
