"""Exhaustive brute-force cross-check of the dynamic-programming optimum.

Every contiguous partition (composition of the channel axis) combined with
every supported spec per group is enumerated independently here, without
using any production helper for the search, and the cheapest feasible layout
is compared byte-for-byte with :func:`select_calibration`.
"""

import itertools
import unittest

from quant_calibration import QuantSpec, evaluate_channel, select_calibration
from quant_calibration.calibration import _align_up


def _partitions(n, allowed):
    """All contiguous tilings of [0, n) using the allowed group lengths."""
    allowed = sorted(set(allowed))

    def build(start):
        if start == n:
            yield ()
            return
        for length in allowed:
            if length > n - start:
                break
            for rest in build(start + length):
                yield ((start, length),) + rest

    yield from build(0)


def _brute_force(
    histograms,
    element_counts,
    specs,
    tolerance_mse,
    header_bytes,
    alignment,
    allowed_spans,
):
    n = len(histograms)
    feasible_layouts = []
    all_layouts = []

    for partition in _partitions(n, allowed_spans):
        # Every group may independently pick any spec.
        choices = itertools.product(specs, repeat=len(partition))
        for group_specs in choices:
            total_bytes = 0
            worst_mse = 0.0
            feasible = True
            for (start, length), spec in zip(partition, group_specs):
                payload = sum(element_counts[start : start + length]) * (
                    spec.bits // 8
                )
                total_bytes += _align_up(header_bytes + payload, alignment)
                for channel in range(start, start + length):
                    error = evaluate_channel(
                        channel,
                        histograms[channel],
                        element_counts[channel],
                        spec,
                    )
                    worst_mse = max(worst_mse, error.mse)
                    if error.mse > tolerance_mse:
                        feasible = False
            layout = (total_bytes, len(partition), worst_mse)
            all_layouts.append(layout)
            if feasible:
                feasible_layouts.append(layout)

    if feasible_layouts:
        return True, min(feasible_layouts)
    return False, min(
        all_layouts,
        key=lambda layout: (
            max(0.0, layout[2] - tolerance_mse),
            layout[0],
            layout[2],
        ),
    )


class TestExhaustiveOptimality(unittest.TestCase):
    def _run_case(self, histograms, counts, bits, scales, tolerance, spans, header, align):
        specs = [
            QuantSpec(b, s)
            for b in bits
            for s in scales
        ]
        expected_feasible, expected = _brute_force(
            histograms,
            counts,
            specs,
            tolerance,
            header,
            align,
            spans,
        )
        result = select_calibration(
            histograms,
            counts,
            bits,
            scales,
            tolerance_mse=tolerance,
            header_bytes=header,
            alignment=align,
            groupable_ranges=spans,
        )
        self.assertEqual(result.feasible, expected_feasible)
        self.assertEqual(result.total_bytes, expected[0])
        self.assertEqual(len(result.groups), expected[1])
        self.assertAlmostEqual(result.max_mse, expected[2], places=12)
        return result

    def test_mixed_channel_data_all_spans(self):
        histograms = [
            {0.1: 30, 0.6: 10},
            {0.5: 25, 1.2: 15},
            {0.0: 20, -0.5: 20},
            {0.5: 10, 4.0: 6},
        ]
        counts = [40, 40, 40, 16]
        self._run_case(
            histograms, counts,
            bits=[8, 16], scales=[0.25, 0.5, 1.0],
            tolerance=0.02, spans=[1, 2, 3, 4],
            header=8, align=8,
        )

    def test_extreme_value_case_matches_brute_force(self):
        histograms = [
            {0.5: 60, 1.0: 40},
            {0.5: 99, 100.0: 1},
            {-0.5: 50, 0.0: 50},
        ]
        counts = [100, 100, 100]
        self._run_case(
            histograms, counts,
            bits=[8, 16], scales=[0.25, 0.5],
            tolerance=0.01, spans=[1, 2, 3],
            header=12, align=16,
        )

    def test_restricted_spans_match_brute_force(self):
        histograms = [
            {0.3: 12, 0.8: 4},
            {1.1: 8, 1.6: 8},
            {-0.2: 10, 0.3: 6},
            {0.7: 20},
        ]
        counts = [16, 16, 16, 20]
        self._run_case(
            histograms, counts,
            bits=[8], scales=[0.1, 0.5],
            tolerance=0.05, spans=[1, 2],
            header=4, align=4,
        )

    def test_infeasible_axis_best_effort_matches_brute_force(self):
        # Only 8-bit and a coarse scale: some channel must violate.
        # 200 and 250 both exceed the int8 ceiling (127 * 0.5 = 63.5).
        histograms = [
            {0.5: 99, 200.0: 1},
            {0.5: 98, 250.0: 2},
        ]
        counts = [100, 100]
        result = self._run_case(
            histograms, counts,
            bits=[8], scales=[0.5],
            tolerance=0.001, spans=[1, 2],
            header=8, align=8,
        )
        self.assertFalse(result.feasible)
        self.assertEqual(set(result.violating_channels), {0, 1})
        # Brute force and the relaxed DP must agree on the minimal violation.
        spec = QuantSpec(8, 0.5)
        worst = max(
            evaluate_channel(i, histograms[i], counts[i], spec).mse
            for i in range(2)
        )
        self.assertAlmostEqual(result.max_mse, worst, places=12)
        # Hand-checked saturated errors:
        # ch0: (200 - 63.5)^2 / 100 = 186.3225
        # ch1: 2 * (250 - 63.5)^2 / 100 = 695.645
        self.assertAlmostEqual(
            result.channel_errors[0].mse, 186.3225, places=6
        )
        self.assertAlmostEqual(
            result.channel_errors[1].mse, 695.645, places=6
        )

    def test_zero_tolerance_exact_data(self):
        histograms = [
            {0.25: 10, 0.75: 10},
            {1.25: 5, 1.75: 5},
            {-0.25: 8, 0.0: 8},
        ]
        counts = [20, 10, 16]
        self._run_case(
            histograms, counts,
            bits=[8, 16], scales=[0.25, 0.5],
            tolerance=0.0, spans=[1, 2, 3],
            header=16, align=32,
        )


if __name__ == "__main__":
    unittest.main()
