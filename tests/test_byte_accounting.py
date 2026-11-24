"""Byte accounting, alignment and cheapest-layout selection checks."""

import unittest

from quant_calibration import CalibrationResult, QuantSpec, select_calibration


# All values in these tables are exact multiples of 0.5, so 8-bit / scale 0.5
# reconstructs them perfectly: byte cost alone drives the selection.
CLEAN_TABLES = [
    {0.5: 50, 1.0: 50},          # channel 0: 100 elements
    {1.5: 40, 2.0: 60},          # channel 1: 100 elements
    {-0.5: 25, 0.0: 75},         # channel 2: 100 elements
]
CLEAN_COUNTS = [100, 100, 100]
BITS = [8, 16]
SCALES = [0.5]


class TestByteAccounting(unittest.TestCase):
    def test_group_bytes_include_header_payload_and_alignment(self):
        # header=12, alignment=16, 8-bit payload=100:
        # align_up(12 + 100, 16) = align_up(112, 16) = 112
        result = select_calibration(
            CLEAN_TABLES,
            CLEAN_COUNTS,
            BITS,
            SCALES,
            tolerance_mse=0.0,
            header_bytes=12,
            alignment=16,
            max_group_size=1,
        )
        self.assertTrue(result.feasible)
        self.assertEqual(len(result.groups), 3)
        for group in result.groups:
            self.assertEqual(group.payload_bytes, 100)
            self.assertEqual(group.total_bytes, 112)
        self.assertEqual(result.total_bytes, 3 * 112)

    def test_alignment_padding_is_charged_per_group(self):
        # header=10, alignment=8, payload=100:
        # align_up(110, 8) = 112 per group.
        result = select_calibration(
            CLEAN_TABLES,
            CLEAN_COUNTS,
            BITS,
            SCALES,
            tolerance_mse=0.0,
            header_bytes=10,
            alignment=8,
            max_group_size=1,
        )
        self.assertEqual(result.groups[0].total_bytes, 112)
        self.assertEqual(result.total_bytes, 336)

    def test_sharing_one_scale_saves_headers(self):
        # One group of three: payload 300 bytes, header 12:
        # align_up(312, 16) = 320 total.
        result = select_calibration(
            CLEAN_TABLES,
            CLEAN_COUNTS,
            BITS,
            SCALES,
            tolerance_mse=0.0,
            header_bytes=12,
            alignment=16,
        )
        self.assertTrue(result.feasible)
        self.assertEqual(len(result.groups), 1)
        group = result.groups[0]
        self.assertEqual(group.channels, (0, 1, 2))
        self.assertEqual(group.bits, 8)
        self.assertEqual(group.scale, 0.5)
        self.assertEqual(group.payload_bytes, 300)
        self.assertEqual(group.total_bytes, 320)
        self.assertEqual(result.total_bytes, 320)
        # Every channel reports zero error under the shared scale.
        self.assertTrue(all(error.mse == 0.0 for error in result.channel_errors))

    def test_16bit_is_chosen_only_when_8bit_cannot_meet_tolerance(self):
        # With the clean tables 8-bit is exact, so even when 16-bit is
        # offered the cheaper width wins.
        result = select_calibration(
            CLEAN_TABLES,
            CLEAN_COUNTS,
            [8, 16],
            SCALES,
            tolerance_mse=0.0,
            header_bytes=4,
            alignment=4,
        )
        self.assertEqual(result.groups[0].bits, 8)

    def test_restricted_groupable_ranges_force_partition_shape(self):
        # Allowed lengths {2} cannot tile 3 channels -> explicit error.
        with self.assertRaises(ValueError):
            select_calibration(
                CLEAN_TABLES,
                CLEAN_COUNTS,
                BITS,
                SCALES,
                tolerance_mse=0.0,
                header_bytes=4,
                alignment=4,
                groupable_ranges=[2],
            )

        # Allowed lengths {1, 2}: the cheapest tiling of three channels is
        # one pair + one singleton (two headers, not three).
        result = select_calibration(
            CLEAN_TABLES,
            CLEAN_COUNTS,
            BITS,
            SCALES,
            tolerance_mse=0.0,
            header_bytes=4,
            alignment=4,
            groupable_ranges=[1, 2],
        )
        self.assertTrue(result.feasible)
        self.assertEqual(len(result.groups), 2)
        self.assertEqual(
            sorted(len(group.channels) for group in result.groups), [1, 2]
        )
        # header 4 + payload: pair -> 204, singleton -> 104
        self.assertEqual(result.total_bytes, 204 + 104)

    def test_duplicate_specs_are_deduplicated(self):
        result = select_calibration(
            CLEAN_TABLES,
            CLEAN_COUNTS,
            [8, 8],
            [0.5, 0.5],
            tolerance_mse=0.0,
            header_bytes=4,
            alignment=4,
        )
        self.assertEqual(len(result.considered_specs), 1)
        self.assertEqual(
            result.considered_specs[0], QuantSpec(bits=8, scale=0.5)
        )

    def test_result_type_and_channel_group_map(self):
        result = select_calibration(
            CLEAN_TABLES,
            CLEAN_COUNTS,
            BITS,
            SCALES,
            tolerance_mse=0.0,
            header_bytes=4,
            alignment=4,
            max_group_size=1,
        )
        self.assertIsInstance(result, CalibrationResult)
        mapping = result.channel_group_map()
        self.assertEqual(set(mapping), {0, 1, 2})
        self.assertEqual([mapping[i].channels for i in range(3)], [(0,), (1,), (2,)])


if __name__ == "__main__":
    unittest.main()
