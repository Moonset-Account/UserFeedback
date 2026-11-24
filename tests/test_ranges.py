import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from disclosure_review.ranges import Range, group_numbers_into_ranges, normalize_ranges


class RangeTests(unittest.TestCase):
    def test_inclusive_one_based_validation(self):
        self.assertEqual(Range(2, 4).length, 3)
        self.assertEqual(list(Range(2, 4).numbers()), [2, 3, 4])
        with self.assertRaises(ValueError):
            Range(0, 2)
        with self.assertRaises(ValueError):
            Range(4, 2)

    def test_normalize_merges_overlapping_and_adjacent(self):
        normalized = normalize_ranges(
            [Range(5, 6), Range(1, 2), Range(2, 3), Range(8, 8)]
        )
        self.assertEqual(normalized, [Range(1, 3), Range(5, 6), Range(8, 8)])

    def test_subtract_preserves_outside_parts(self):
        self.assertEqual(Range(1, 6).subtract([Range(2, 3)]), [Range(1, 1), Range(4, 6)])
        self.assertEqual(Range(1, 4).subtract([Range(1, 4)]), [])
        self.assertEqual(Range(1, 5).subtract([Range(2, 2), Range(4, 4)]), [
            Range(1, 1),
            Range(3, 3),
            Range(5, 5),
        ])

    def test_group_numbers(self):
        self.assertEqual(
            group_numbers_into_ranges([1, 2, 3, 7, 8, 10]),
            [Range(1, 3), Range(7, 8), Range(10, 10)],
        )


if __name__ == "__main__":
    unittest.main()
