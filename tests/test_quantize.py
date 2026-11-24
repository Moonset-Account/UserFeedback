"""Hand-computed checks for nearest quantization and weighted SSE."""

import math
import unittest

from quant_calibration import QuantSpec, evaluate_channel, quantize_dequantize


class TestQuantizeDequantize(unittest.TestCase):
    def test_8bit_nearest_quantization_half_away_from_zero(self):
        spec = QuantSpec(bits=8, scale=0.5)
        # Signed 8-bit levels are [-128, 127], step 0.5.
        result = quantize_dequantize(
            [0.0, 0.24, 0.25, 0.26, 0.75, -0.25, -0.75, 63.5], spec
        )
        self.assertEqual(
            result,
            [
                0.0,   # 0
                0.0,   # round(0.48) = 0
                0.5,   # exact half -> away from zero -> +1
                0.5,   # round(0.52) = 1
                1.0,   # round(1.5) = 2 (half away from zero, not banker's)
                -0.5,  # exact half -> -1
                -1.0,  # round(-1.5) = -2
                63.5,  # exact representation
            ],
        )

    def test_saturation_truncation_at_signed_limits(self):
        spec = QuantSpec(bits=8, scale=1.0)
        result = quantize_dequantize([200.0, -200.0, 127.0, -128.0], spec)
        self.assertEqual(result, [127.0, -128.0, 127.0, -128.0])

    def test_16bit_has_wider_signed_range(self):
        spec = QuantSpec(bits=16, scale=1.0)
        result = quantize_dequantize([200.0, -200.0], spec)
        self.assertEqual(result, [200.0, -200.0])

    def test_repeated_values_are_independent(self):
        spec = QuantSpec(bits=8, scale=0.1)
        result = quantize_dequantize([0.14, 0.14, 0.16], spec)
        self.assertEqual(result, [0.1, 0.1, 0.2])


class TestEvaluateChannel(unittest.TestCase):
    def test_frequency_weighted_sse_hand_calculation(self):
        # scale 0.5: x=0.2 -> 0.0 err=-0.2; x=0.8 -> 1.0 err=+0.2
        # sse = 10*0.04 + 10*0.04 = 0.8 ; mse = 0.8 / 20 = 0.04
        spec = QuantSpec(bits=8, scale=0.5)
        error = evaluate_channel(
            channel=0,
            histogram={0.2: 10, 0.8: 10},
            element_count=20,
            spec=spec,
        )
        self.assertAlmostEqual(error.sse, 0.8, places=12)
        self.assertAlmostEqual(error.mse, 0.04, places=12)
        self.assertEqual(error.saturated_count, 0)
        self.assertAlmostEqual(error.max_abs_error, 0.2, places=12)
        self.assertEqual(error.element_count, 20)
        self.assertEqual(error.bits, 8)
        self.assertEqual(error.scale, 0.5)

    def test_exact_reconstruction_gives_zero_error(self):
        spec = QuantSpec(bits=8, scale=0.5)
        error = evaluate_channel(
            channel=3,
            histogram=[(0.5, 4), (1.0, 7), (-1.5, 2)],
            element_count=13,
            spec=spec,
        )
        self.assertEqual(error.sse, 0.0)
        self.assertEqual(error.mse, 0.0)
        self.assertEqual(error.max_abs_error, 0.0)
        self.assertEqual(error.saturated_count, 0)

    def test_saturation_counts_and_error(self):
        # 8-bit, scale 1.0: level ceiling 127. x=200 saturates (400 elems),
        # x=-150 saturates to -128 (100 elems). x=50 exact.
        spec = QuantSpec(bits=8, scale=1.0)
        error = evaluate_channel(
            channel=1,
            histogram={200.0: 400, 50.0: 50, -150.0: 100},
            element_count=550,
            spec=spec,
        )
        # sse = 400 * 73^2 + 100 * 22^2
        expected_sse = 400 * 73**2 + 100 * 22**2
        self.assertEqual(error.saturated_count, 500)
        self.assertAlmostEqual(error.sse, float(expected_sse), places=6)
        self.assertAlmostEqual(error.mse, expected_sse / 550, places=6)
        self.assertAlmostEqual(error.max_abs_error, 73.0, places=6)

    def test_mse_normalizes_by_element_count(self):
        spec = QuantSpec(bits=8, scale=0.5)
        small = evaluate_channel(
            0, {0.2: 10, 0.8: 10}, 20, spec
        )
        big = evaluate_channel(
            0, {0.2: 1000, 0.8: 1000}, 2000, spec
        )
        # Same per-element distortion: MSE identical, SSE scales 100x.
        self.assertAlmostEqual(small.mse, big.mse, places=12)
        self.assertAlmostEqual(big.sse, 100.0 * small.sse, places=6)

    def test_histogram_count_mismatch_is_rejected(self):
        spec = QuantSpec(bits=8, scale=1.0)
        with self.assertRaises(ValueError):
            evaluate_channel(0, {1.0: 3}, element_count=4, spec=spec)

    def test_negative_frequency_is_rejected(self):
        spec = QuantSpec(bits=8, scale=1.0)
        with self.assertRaises(ValueError):
            evaluate_channel(0, [(1.0, -1)], element_count=1, spec=spec)

    def test_float_frequency_keys_are_supported(self):
        spec = QuantSpec(bits=8, scale=0.25)
        error = evaluate_channel(
            0, {0.1: 2, 0.4: 2}, element_count=4, spec=spec
        )
        # 0.1 -> 0.0 (err 0.1); 0.4 -> 0.5 (err 0.1)
        self.assertTrue(math.isclose(error.sse, 4 * 0.01, rel_tol=1e-12))


if __name__ == "__main__":
    unittest.main()
