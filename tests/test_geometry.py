import unittest

from safe_dump_planner.geometry import (
    ByteRange,
    intersect_unions,
    normalize_ranges,
    subtract_unions,
    uncovered_parts,
)
from safe_dump_planner.errors import ValidationError


class GeometryTests(unittest.TestCase):
    def test_half_open_ranges_merge_overlaps_and_adjacency(self):
        ranges = normalize_ranges([(0, 8), [8, 16], ByteRange(12, 24), (30, 30)])
        self.assertEqual(ranges, (ByteRange(0, 24),))

    def test_separate_ranges_stay_separate(self):
        ranges = normalize_ranges([(0, 8), (9, 16)])
        self.assertEqual(ranges, (ByteRange(0, 8), ByteRange(9, 16)))

    def test_page_boundary_is_valid_but_past_end_is_not(self):
        self.assertEqual(
            normalize_ranges([(4090, 4096)], page_size=4096),
            (ByteRange(4090, 4096),),
        )
        with self.assertRaises(ValidationError):
            normalize_ranges([(4090, 4097)], page_size=4096)
        with self.assertRaises(ValidationError):
            ByteRange(5, 4)

    def test_intersection_of_policy_unions(self):
        left = normalize_ranges([(0, 128), (200, 256)])
        right = normalize_ranges([(64, 192), (210, 220)])
        self.assertEqual(
            intersect_unions(left, right),
            (ByteRange(64, 128), ByteRange(210, 220)),
        )

    def test_subtract_and_uncovered_parts(self):
        source = normalize_ranges([(0, 100)])
        denied = normalize_ranges([(10, 20), (40, 60)])
        self.assertEqual(
            subtract_unions(source, denied),
            (ByteRange(0, 10), ByteRange(20, 40), ByteRange(60, 100)),
        )
        self.assertEqual(
            uncovered_parts([ByteRange(0, 30)], normalize_ranges([(5, 25)])),
            (ByteRange(0, 5), ByteRange(25, 30)),
        )


if __name__ == "__main__":
    unittest.main()
