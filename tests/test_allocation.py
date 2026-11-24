from __future__ import annotations

import unittest
from decimal import Decimal
from fractions import Fraction

from error_budget.allocation import (
    allocate_with_pending,
    diff_allocations,
    largest_remainder_allocation,
)
from error_budget.errors import InvalidAmountError, InvalidWeightsError


class LargestRemainderTests(unittest.TestCase):
    def test_equal_weights_use_incident_id_for_tied_remainders(self) -> None:
        # Four units over three equal weights: one remainder unit.
        self.assertEqual(
            largest_remainder_allocation(4, {"C": 1, "A": 1, "B": 1}),
            {"A": 2, "B": 1, "C": 1},
        )

        # Five units: two remainder units, both awarded by id order.
        self.assertEqual(
            largest_remainder_allocation(5, {"C": 1, "A": 1, "B": 1}),
            {"A": 2, "B": 2, "C": 1},
        )

    def test_plain_weight_sequence_uses_numeric_index_ids(self) -> None:
        self.assertEqual(
            largest_remainder_allocation(4, [1, 1, 1]),
            {0: 2, 1: 1, 2: 1},
        )
        self.assertEqual(
            largest_remainder_allocation(4, {3: 1, 1: 1, 2: 1}),
            {1: 2, 2: 1, 3: 1},
        )

    def test_highest_fractional_parts_receive_remainders(self) -> None:
        self.assertEqual(
            largest_remainder_allocation(10, {"A": 1, "B": 2}),
            {"A": 3, "B": 7},
        )

        result = largest_remainder_allocation(10, {"A": 7, "B": 4, "C": 1})
        self.assertEqual(result, {"A": 6, "B": 3, "C": 1})
        self.assertEqual(sum(result.values()), 10)

    def test_many_remainder_seats_are_ranked_by_fraction_then_id(self) -> None:
        # total=8, equal four weights: quotas=2 exactly, no remainder.
        self.assertEqual(
            largest_remainder_allocation(8, {"D": 1, "C": 1, "B": 1, "A": 1}),
            {"A": 2, "B": 2, "C": 2, "D": 2},
        )
        # total=10: floors=2 each, two seats; all fractions tie at 0.5.
        self.assertEqual(
            largest_remainder_allocation(10, {"D": 1, "C": 1, "B": 1, "A": 1}),
            {"A": 3, "B": 3, "C": 2, "D": 2},
        )

    def test_exact_decimal_and_fraction_weights_avoid_float_error(self) -> None:
        weights = {"A": Decimal("0.1"), "B": "0.2", "C": Fraction(7, 10)}
        self.assertEqual(
            largest_remainder_allocation(3, weights),
            {"A": 0, "B": 1, "C": 2},
        )

        # The visible decimal float values are normalized via their decimal text.
        self.assertEqual(
            largest_remainder_allocation(3, {"A": 0.1, "B": 0.2, "C": 0.7}),
            {"A": 0, "B": 1, "C": 2},
        )

    def test_zero_weight_incident_receives_zero(self) -> None:
        self.assertEqual(
            largest_remainder_allocation(10, {"A": 1, "B": 0}),
            {"A": 10, "B": 0},
        )

    def test_zero_total_returns_zero_for_every_known_incident(self) -> None:
        self.assertEqual(
            largest_remainder_allocation(0, {"A": 1, "B": 1}),
            {"A": 0, "B": 0},
        )

    def test_missing_or_unallocatable_weights_become_pending(self) -> None:
        self.assertEqual(allocate_with_pending(10, None), ({}, 10))
        allocation, pending = allocate_with_pending(10, {"A": 0, "B": 0})
        self.assertEqual(allocation, {"A": 0, "B": 0})
        self.assertEqual(pending, 10)

        with self.assertRaises(InvalidWeightsError):
            largest_remainder_allocation(10, {})
        with self.assertRaises(InvalidWeightsError):
            largest_remainder_allocation(10, {"A": 0})

    def test_invalid_amounts_and_weights_are_rejected(self) -> None:
        for bad_amount in (-1, True, 1.2, "1.25"):
            with self.subTest(bad_amount=bad_amount):
                with self.assertRaises(InvalidAmountError):
                    largest_remainder_allocation(bad_amount, {"A": 1})

        for bad_weights in ({"A": -1}, {"A": "not-a-number"}, "A:1"):
            with self.subTest(bad_weights=bad_weights):
                with self.assertRaises(InvalidWeightsError):
                    largest_remainder_allocation(10, bad_weights)  # type: ignore[arg-type]

    def test_allocation_diff_only_contains_nonzero_changes(self) -> None:
        self.assertEqual(
            diff_allocations({"A": 50, "B": 50}, {"A": 67, "B": 33}),
            {"A": 17, "B": -17},
        )
        self.assertEqual(diff_allocations({"A": 5}, {"A": 5}), {})
        self.assertEqual(
            diff_allocations({"A": 5, "B": 5}, {"B": 10}),
            {"A": -5, "B": 5},
        )


if __name__ == "__main__":
    unittest.main()
