import unittest

from frame_occlusion.geometry import Rect, subtract_rect


class RectTests(unittest.TestCase):
    def test_intersection_and_boundary_only_contact(self):
        from frame_occlusion.geometry import intersection

        a = Rect(0, 0, 4, 3)
        self.assertEqual(intersection(a, Rect(2, 1, 4, 2)), Rect(2, 1, 2, 2))
        self.assertIsNone(intersection(a, Rect(4, 0, 2, 3)))
        self.assertIsNone(intersection(a, Rect(0, 3, 4, 1)))

    def test_subtract_center_produces_four_pieces(self):
        outer = Rect(0, 0, 4, 4)
        pieces = subtract_rect(outer, Rect(1, 1, 2, 2))
        self.assertEqual(
            pieces,
            [
                Rect(0, 0, 4, 1),
                Rect(0, 3, 4, 1),
                Rect(0, 1, 1, 2),
                Rect(3, 1, 1, 2),
            ],
        )

    def test_subtract_edge_and_containing_hole(self):
        outer = Rect(0, 0, 4, 3)
        self.assertEqual(
            subtract_rect(outer, Rect(3, 0, 4, 3)),
            [Rect(0, 0, 3, 3)],
        )
        self.assertEqual(subtract_rect(outer, outer), [])
        self.assertEqual(subtract_rect(outer, Rect(9, 9, 1, 1)), [outer])

    def test_negative_size_rejected(self):
        with self.assertRaises(ValueError):
            Rect(0, 0, -1, 2)


if __name__ == "__main__":
    unittest.main()
