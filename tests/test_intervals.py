import unittest

from volume_audit.intervals import (
    Interval,
    calculate_coverage,
    coerce_interval,
    merge_intervals,
    subtract_covered,
)


class IntervalTests(unittest.TestCase):
    def test_mapping_aliases_and_size(self):
        interval = coerce_interval({"start_offset": 10, "length": 40})
        self.assertEqual(interval, Interval(10, 50))

    def test_inclusive_last_block_is_converted_to_exclusive_end(self):
        interval = coerce_interval({"start_lba": 0, "last_lba": 99})
        self.assertEqual(interval, Interval(0, 100))

    def test_reversed_interval_is_rejected(self):
        with self.assertRaises(ValueError):
            Interval(10, 9)

    def test_merge_adjacent_overlapping_out_of_order_and_duplicates(self):
        merged = merge_intervals(
            [
                (80, 100),
                {"start": 0, "end": 25},
                (20, 50),  # overlaps and extends
                (50, 80),  # adjacent to previous and next
                (80, 100),  # duplicate
            ]
        )
        self.assertEqual([item.to_dict() for item in merged], [{"start": 0, "end": 100}])

    def test_exact_boundary_touch_completes_coverage(self):
        coverage = calculate_coverage([0, 100], [[0, 50], [50, 100]])
        self.assertTrue(coverage["fully_covered"])
        self.assertEqual(coverage["uncovered_ranges"], [])
        self.assertEqual(coverage["coverage_ratio"], 1.0)

    def test_receipt_extending_beyond_target_is_clipped(self):
        coverage = calculate_coverage({"start": 20, "end": 80}, [[0, 100]])
        self.assertTrue(coverage["fully_covered"])
        self.assertEqual(coverage["covered_ranges"], [{"start": 20, "end": 80}])

    def test_internal_start_and_end_gaps_are_reported_exactly(self):
        gaps = subtract_covered([0, 100], [[10, 40], [60, 90]])
        self.assertEqual(
            [item.to_dict() for item in gaps],
            [
                {"start": 0, "end": 10},
                {"start": 40, "end": 60},
                {"start": 90, "end": 100},
            ],
        )

    def test_zero_length_target_has_no_uncovered_range(self):
        coverage = calculate_coverage([5, 5], [])
        self.assertEqual(coverage["uncovered_ranges"], [])
        self.assertFalse(coverage["fully_covered"])
        self.assertEqual(coverage["coverage_ratio"], 0.0)


if __name__ == "__main__":
    unittest.main()
