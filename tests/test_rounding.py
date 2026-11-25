from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from decimal_kernel import rescale_to_target_scale


class DirectRoundingTests(unittest.TestCase):
    def test_exact_midpoints_tie_to_even(self) -> None:
        # scale 1 -> scale 0: factor 10, remainder 5 is the exact midpoint.
        self.assertEqual(rescale_to_target_scale(15, 1, 0), 2)   # 1.5 -> 2
        self.assertEqual(rescale_to_target_scale(25, 1, 0), 2)   # 2.5 -> 2
        self.assertEqual(rescale_to_target_scale(-15, 1, 0), -2)
        self.assertEqual(rescale_to_target_scale(-25, 1, 0), -2)

    def test_values_on_both_sides_of_midpoint(self) -> None:
        # Source scale 3; 1499/1000 = 1.499 and 1501/1000 = 1.501.
        cases = [
            (1499, 1),
            (1500, 2),  # exact midpoint
            (1501, 2),
            (-1499, -1),
            (-1500, -2),
            (-1501, -2),
        ]
        for mantissa, expected in cases:
            with self.subTest(mantissa=mantissa):
                self.assertEqual(
                    rescale_to_target_scale(mantissa, 3, 0), expected
                )

    def test_direct_rounding_avoids_double_rounding(self) -> None:
        # 1.499 is directly closer to 1 than to 2.
        direct = rescale_to_target_scale(1499, 3, 0)
        self.assertEqual(direct, 1)

        # A forbidden two-stage path would first produce 1.50 (mantissa 150 at
        # scale 2), then tie that to even integer 2.
        first_stage = rescale_to_target_scale(1499, 3, 2)
        self.assertEqual(first_stage, 150)
        second_stage = rescale_to_target_scale(first_stage, 2, 0)
        self.assertEqual(second_stage, 2)
        self.assertNotEqual(direct, second_stage)

    def test_long_remainder_is_one_decision(self) -> None:
        # 1.000049999 is below the 1.00005 midpoint between scaled ints 10000
        # and 10001 at target scale 4.
        self.assertEqual(
            rescale_to_target_scale(1_000_049_999, 9, 4),
            10000,
        )
        # 1.000050000 is the exact midpoint; 10000 is already even.
        self.assertEqual(
            rescale_to_target_scale(1_000_050_000, 9, 4),
            10000,
        )
        # 1.000050001 is above the midpoint and rounds to 10001.
        self.assertEqual(
            rescale_to_target_scale(1_000_050_001, 9, 4),
            10001,
        )

    def test_scale_expansion_is_exact(self) -> None:
        self.assertEqual(rescale_to_target_scale(123, 2, 5), 123000)
        self.assertEqual(rescale_to_target_scale(-123, 2, 5), -123000)

    def test_negative_tiny_value_normalizes_to_positive_zero(self) -> None:
        self.assertEqual(rescale_to_target_scale(-1, 3, 0), 0)
        result = rescale_to_target_scale(-1, 3, 0)
        self.assertIs(result, 0)


if __name__ == "__main__":
    unittest.main()
