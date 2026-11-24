"""Extreme-value regrouping, interleaved channels and boundary scenarios."""

import unittest

from quant_calibration import select_calibration

# Three contiguous channels of 100 elements each.  Channel 1 contains one
# extreme value (100.0) that cannot share the 8-bit / 0.5 scale used by its
# ordinary neighbors without saturating.
TABLES = [
    {0.5: 60, 1.0: 40},            # channel 0, ordinary
    {0.5: 99, 100.0: 1},           # channel 1, one extreme value
    {-0.5: 50, 0.0: 50},           # channel 2, ordinary
]
COUNTS = [100, 100, 100]
HEADER = 12
ALIGN = 16


def extreme_error(result, channel):
    return result.channel_errors[channel]


class TestExtremeValueRegrouping(unittest.TestCase):
    def test_shared_scale_pushes_neighbor_over_tolerance(self):
        # One shared 8-bit group, scale 0.5: channels 0 and 2 are exact,
        # channel 1 saturates its single extreme value (qmax=127 -> 63.5).
        # Hand calculation: sse = (100 - 63.5)^2 = 1332.25, mse = 13.3225.
        result = select_calibration(
            TABLES,
            COUNTS,
            supported_bits=[8],
            supported_scales=[0.5],
            tolerance_mse=0.01,
            header_bytes=HEADER,
            alignment=ALIGN,
        )
        # Not feasible, and the best-effort layout keeps full error evidence.
        self.assertFalse(result.feasible)
        self.assertEqual(result.violating_channels, (1,))
        err1 = extreme_error(result, 1)
        self.assertAlmostEqual(err1.sse, 1332.25, places=6)
        self.assertAlmostEqual(err1.mse, 13.3225, places=6)
        self.assertEqual(err1.saturated_count, 1)
        self.assertAlmostEqual(err1.max_abs_error, 36.5, places=6)
        self.assertEqual(extreme_error(result, 0).mse, 0.0)
        self.assertEqual(extreme_error(result, 2).mse, 0.0)

    def test_extreme_channel_is_isolated_with_a_wider_width(self):
        # Offering 16-bit lets the optimizer regroup: the extreme channel
        # gets its own 16-bit group while ordinary channels keep cheap 8-bit
        # groups, instead of widening the whole axis to 16-bit.
        result = select_calibration(
            TABLES,
            COUNTS,
            supported_bits=[8, 16],
            supported_scales=[0.5],
            tolerance_mse=0.01,
            header_bytes=HEADER,
            alignment=ALIGN,
        )
        self.assertTrue(result.feasible)
        self.assertEqual(result.violating_channels, ())

        bits_by_channel = {
            channel: group.bits
            for group in result.groups
            for channel in group.channels
        }
        self.assertEqual(bits_by_channel, {0: 8, 1: 16, 2: 8})
        self.assertEqual(len(result.groups), 3)

        # Byte audit:
        # 8-bit singleton: align_up(12 + 100, 16) = 112
        # 16-bit singleton: align_up(12 + 200, 16) = 224
        totals = sorted(group.total_bytes for group in result.groups)
        self.assertEqual(totals, [112, 112, 224])
        self.assertEqual(result.total_bytes, 448)

        # A single widened 16-bit group would cost align_up(12+600, 16)=624.
        one_group = select_calibration(
            TABLES,
            COUNTS,
            supported_bits=[8, 16],
            supported_scales=[0.5],
            tolerance_mse=0.01,
            header_bytes=HEADER,
            alignment=ALIGN,
            groupable_ranges=[3],
        )
        self.assertTrue(one_group.feasible)
        self.assertEqual(one_group.groups[0].bits, 16)
        self.assertEqual(one_group.total_bytes, 624)
        self.assertLess(result.total_bytes, one_group.total_bytes)

        # Every channel now meets the tolerance, including the extreme one.
        for error in result.channel_errors:
            self.assertLessEqual(error.mse, 0.01)
        self.assertEqual(result.max_mse, 0.0)

    def test_loose_tolerance_keeps_the_single_shared_group(self):
        # Same data, tolerance exactly at the saturated channel's MSE:
        # boundary equality counts as passing, so one cheap 8-bit group wins.
        result = select_calibration(
            TABLES,
            COUNTS,
            supported_bits=[8, 16],
            supported_scales=[0.5],
            tolerance_mse=13.3225,
            header_bytes=HEADER,
            alignment=ALIGN,
        )
        self.assertTrue(result.feasible)
        self.assertEqual(len(result.groups), 1)
        self.assertEqual(result.groups[0].bits, 8)
        # align_up(12 + 300, 16) = 320
        self.assertEqual(result.total_bytes, 320)

    def test_tight_tolerance_just_below_boundary_forces_split(self):
        result = select_calibration(
            TABLES,
            COUNTS,
            supported_bits=[8, 16],
            supported_scales=[0.5],
            tolerance_mse=13.3224,
            header_bytes=HEADER,
            alignment=ALIGN,
        )
        self.assertTrue(result.feasible)
        self.assertEqual(len(result.groups), 3)
        self.assertEqual(result.total_bytes, 448)


class TestInterleavedExtremeChannels(unittest.TestCase):
    def test_alternating_extreme_channels_each_get_isolated(self):
        # Four channels; extreme values sit on channels 1 and 3.
        tables = [
            {0.5: 100},
            {0.5: 99, 100.0: 1},
            {-1.0: 100},
            {0.5: 95, 80.0: 5},
        ]
        counts = [100, 100, 100, 100]
        result = select_calibration(
            tables,
            counts,
            supported_bits=[8, 16],
            supported_scales=[0.5],
            tolerance_mse=0.01,
            header_bytes=HEADER,
            alignment=ALIGN,
        )
        self.assertTrue(result.feasible)
        bits_by_channel = {
            channel: group.bits
            for group in result.groups
            for channel in group.channels
        }
        self.assertEqual(bits_by_channel, {0: 8, 1: 16, 2: 8, 3: 16})
        # 2 * 112 + 2 * 224 = 672
        self.assertEqual(result.total_bytes, 672)
        # No group mixes an 8-bit ordinary channel with a saturated neighbor.
        for group in result.groups:
            self.assertEqual(
                len({error.bits for error in group.channel_errors}), 1
            )

    def test_pairing_ordinary_with_extreme_is_more_expensive(self):
        tables = [
            {0.5: 100},
            {0.5: 99, 100.0: 1},
            {-1.0: 100},
            {0.5: 95, 80.0: 5},
        ]
        counts = [100, 100, 100, 100]
        # Force pairs: both pairs must run at 16-bit, each pair costs
        # align_up(12 + 400, 16) = 416, total 832.
        paired = select_calibration(
            tables,
            counts,
            supported_bits=[8, 16],
            supported_scales=[0.5],
            tolerance_mse=0.01,
            header_bytes=HEADER,
            alignment=ALIGN,
            groupable_ranges=[2],
        )
        self.assertTrue(paired.feasible)
        self.assertEqual([g.bits for g in paired.groups], [16, 16])
        self.assertEqual(paired.total_bytes, 832)

        free = select_calibration(
            tables,
            counts,
            supported_bits=[8, 16],
            supported_scales=[0.5],
            tolerance_mse=0.01,
            header_bytes=HEADER,
            alignment=ALIGN,
        )
        self.assertLess(free.total_bytes, paired.total_bytes)


class TestBoundariesAndValidation(unittest.TestCase):
    def test_single_channel_axis(self):
        result = select_calibration(
            [{0.25: 4, 0.75: 4}],
            [8],
            supported_bits=[8],
            supported_scales=[0.25],
            tolerance_mse=0.0,
            header_bytes=8,
            alignment=8,
        )
        self.assertTrue(result.feasible)
        self.assertEqual(result.groups[0].channels, (0,))
        # align_up(8 + 8, 8) = 16
        self.assertEqual(result.total_bytes, 16)

    def test_all_zero_histogram_is_exact_under_any_scale(self):
        tables = [{0.0: 64}, {0.0: 32}]
        result = select_calibration(
            tables,
            [64, 32],
            supported_bits=[8, 16],
            supported_scales=[0.1, 1.0],
            tolerance_mse=0.0,
            header_bytes=4,
            alignment=4,
        )
        self.assertTrue(result.feasible)
        # Cheapest: one 8-bit group, payload 96 + header 4 = 100.
        self.assertEqual(result.groups[0].bits, 8)
        self.assertEqual(result.groups[0].scale, 0.1)
        self.assertEqual(result.total_bytes, 100)
        self.assertTrue(all(e.mse == 0.0 for e in result.channel_errors))

    def test_repeated_calls_are_deterministic(self):
        kwargs = dict(
            histograms=TABLES,
            element_counts=COUNTS,
            supported_bits=[8, 16],
            supported_scales=[0.5, 0.25],
            tolerance_mse=0.01,
            header_bytes=HEADER,
            alignment=ALIGN,
        )
        first = select_calibration(**kwargs)
        second = select_calibration(**kwargs)
        self.assertEqual(first, second)

    def test_non_byte_aligned_width_rejected(self):
        with self.assertRaises(ValueError):
            select_calibration(
                TABLES,
                COUNTS,
                supported_bits=[4],
                supported_scales=[0.5],
                tolerance_mse=0.01,
                header_bytes=HEADER,
                alignment=ALIGN,
            )

    def test_non_positive_scale_rejected(self):
        with self.assertRaises(ValueError):
            select_calibration(
                TABLES,
                COUNTS,
                supported_bits=[8],
                supported_scales=[0.0],
                tolerance_mse=0.01,
                header_bytes=HEADER,
                alignment=ALIGN,
            )

    def test_bad_alignment_and_header_rejected(self):
        with self.assertRaises(ValueError):
            select_calibration(
                TABLES,
                COUNTS,
                supported_bits=[8],
                supported_scales=[0.5],
                tolerance_mse=0.01,
                header_bytes=HEADER,
                alignment=0,
            )
        with self.assertRaises(ValueError):
            select_calibration(
                TABLES,
                COUNTS,
                supported_bits=[8],
                supported_scales=[0.5],
                tolerance_mse=0.01,
                header_bytes=-1,
                alignment=ALIGN,
            )

    def test_empty_axis_and_count_mismatch_rejected(self):
        with self.assertRaises(ValueError):
            select_calibration(
                [],
                [],
                supported_bits=[8],
                supported_scales=[0.5],
                tolerance_mse=0.01,
                header_bytes=HEADER,
                alignment=ALIGN,
            )
        with self.assertRaises(ValueError):
            select_calibration(
                TABLES,
                [100, 100],  # one entry short
                supported_bits=[8],
                supported_scales=[0.5],
                tolerance_mse=0.01,
                header_bytes=HEADER,
                alignment=ALIGN,
            )

    def test_span_longer_than_axis_rejected(self):
        with self.assertRaises(ValueError):
            select_calibration(
                TABLES,
                COUNTS,
                supported_bits=[8],
                supported_scales=[0.5],
                tolerance_mse=0.01,
                header_bytes=HEADER,
                alignment=ALIGN,
                groupable_ranges=[1, 4],
            )

    def test_negative_tolerance_rejected(self):
        with self.assertRaises(ValueError):
            select_calibration(
                TABLES,
                COUNTS,
                supported_bits=[8],
                supported_scales=[0.5],
                tolerance_mse=-0.1,
                header_bytes=HEADER,
                alignment=ALIGN,
            )


if __name__ == "__main__":
    unittest.main()
