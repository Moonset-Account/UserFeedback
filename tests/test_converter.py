"""Synthetic-data unit tests for the histogram delta conversion module.

These tests use only locally constructed data; they perform no I/O and
no network access.
"""

import math
import unittest

from histogram_delta import (
    INF,
    BaselineEstablished,
    ConflictingRetransmissionError,
    HistogramDeltaConverter,
    InvalidSampleError,
    NegativeIncrementError,
    OutOfOrderTimestampError,
    ProcessSample,
    Retransmission,
    Settlement,
    compute_increments,
)


def make_sample(
    process_id,
    process_start,
    timestamp,
    upper_bounds,
    cumulative_counts,
):
    return ProcessSample.build(
        process_id,
        process_start,
        timestamp,
        upper_bounds,
        cumulative_counts,
    )


class TestFirstSampleAndBaseline(unittest.TestCase):
    def test_first_sample_only_establishes_baseline(self):
        converter = HistogramDeltaConverter()
        result = converter.submit(
            make_sample("p1", "start-a", 1, (10, 20, INF), (3, 7, 10))
        )
        self.assertIsInstance(result, BaselineEstablished)
        self.assertEqual(result.process_id, "p1")
        self.assertEqual(result.timestamp, 1)
        self.assertIn("p1", converter.tracked_processes())
        baseline = converter.baseline_for("p1")
        self.assertEqual(baseline.timestamp, 1)
        self.assertEqual(len(converter), 1)


class TestIdenticalBucketSettlement(unittest.TestCase):
    def test_increments_on_stable_buckets(self):
        converter = HistogramDeltaConverter()
        converter.submit(
            make_sample("p1", "start-a", 1, (10, 20, INF), (3, 7, 10))
        )
        result = converter.submit(
            make_sample("p1", "start-a", 2, (10, 20, INF), (5, 10, 14))
        )
        self.assertIsInstance(result, Settlement)
        self.assertFalse(result.boundaries_changed)
        self.assertEqual(result.common_upper_bounds, (10, 20, INF))
        self.assertEqual(
            [(item.lower_bound, item.upper_bound, item.new_requests)
             for item in result.increments],
            [(0.0, 10, 2), (10, 20, 1), (20, INF, 1)],
        )
        self.assertEqual(result.total_new_requests, 4)
        # The good sample becomes the new baseline.
        self.assertEqual(converter.baseline_for("p1").timestamp, 2)

    def test_zero_increments_are_valid(self):
        converter = HistogramDeltaConverter()
        converter.submit(
            make_sample("p1", "start-a", 1, (10, INF), (5, 9))
        )
        result = converter.submit(
            make_sample("p1", "start-a", 2, (10, INF), (5, 9))
        )
        self.assertIsInstance(result, Settlement)
        self.assertEqual([item.new_requests for item in result.increments], [0, 0])
        self.assertEqual(result.total_new_requests, 0)

    def test_three_sample_chain_uses_most_recent_baseline(self):
        converter = HistogramDeltaConverter()
        converter.submit(
            make_sample("p1", "start-a", 1, (10, 20, INF), (3, 7, 10))
        )
        converter.submit(
            make_sample("p1", "start-a", 2, (10, 20, INF), (5, 10, 14))
        )
        result = converter.submit(
            make_sample("p1", "start-a", 3, (10, 20, INF), (5, 12, 20))
        )
        # Deltas against timestamp 2: cumulative 0, 2, 6 -> intervals 0, 2, 4.
        self.assertEqual(
            [item.new_requests for item in result.increments], [0, 2, 4]
        )
        self.assertEqual(result.total_new_requests, 6)
        self.assertEqual(result.baseline_timestamp, 2)


class TestBoundaryChanges(unittest.TestCase):
    def test_only_shared_upper_bounds_are_settled(self):
        converter = HistogramDeltaConverter()
        converter.submit(
            make_sample("p1", "start-a", 1, (10, 20, INF), (3, 7, 10))
        )
        # Bound 10 disappears, bound 15 appears; only 20 and +inf are shared.
        result = converter.submit(
            make_sample("p1", "start-a", 2, (15, 20, INF), (6, 9, 14))
        )
        self.assertIsInstance(result, Settlement)
        self.assertTrue(result.boundaries_changed)
        self.assertEqual(result.common_upper_bounds, (20, INF))
        self.assertEqual(
            [(item.lower_bound, item.upper_bound, item.new_requests)
             for item in result.increments],
            [(0.0, 20, 2), (20, INF, 2)],
        )
        # No request was guessed into the new (0, 15] / (15, 20] buckets.
        self.assertEqual(len(result.increments), 2)

    def test_shared_bounds_follow_new_sample_order_when_more_bounds(self):
        converter = HistogramDeltaConverter()
        converter.submit(
            make_sample("p1", "start-a", 1, (10, INF), (3, 10))
        )
        result = converter.submit(
            make_sample("p1", "start-a", 2, (5, 10, INF), (4, 5, 14))
        )
        self.assertTrue(result.boundaries_changed)
        self.assertEqual(result.common_upper_bounds, (10, INF))
        self.assertEqual(
            [(item.lower_bound, item.upper_bound, item.new_requests)
             for item in result.increments],
            [(0.0, 10, 2), (10, INF, 4)],
        )

    def test_only_infinity_shared_settles_total(self):
        converter = HistogramDeltaConverter()
        converter.submit(
            make_sample("p1", "start-a", 1, (10, INF), (3, 10))
        )
        result = converter.submit(
            make_sample("p1", "start-a", 2, (20, INF), (8, 14))
        )
        self.assertEqual(result.common_upper_bounds, (INF,))
        self.assertEqual(len(result.increments), 1)
        self.assertEqual(result.increments[0].lower_bound, 0.0)
        self.assertEqual(result.increments[0].upper_bound, INF)
        self.assertEqual(result.increments[0].new_requests, 4)


class TestNegativeIncrementRejection(unittest.TestCase):
    def test_negative_interval_rejects_sample_and_keeps_baseline(self):
        converter = HistogramDeltaConverter()
        first = make_sample("p1", "start-a", 1, (10, 20, INF), (3, 7, 10))
        converter.submit(first)

        bad = make_sample("p1", "start-a", 2, (10, 20, INF), (5, 6, 12))
        with self.assertRaises(NegativeIncrementError) as caught:
            converter.submit(bad)
        error = caught.exception
        # Cumulative deltas 2, -1, 2 -> second interval (10, 20] gets -3.
        self.assertEqual(error.lower_bound, 10)
        self.assertEqual(error.upper_bound, 20)
        self.assertEqual(error.interval_increment, -3)

        # The rejected sample did not replace the retained baseline.
        retained = converter.baseline_for("p1")
        self.assertEqual(retained.timestamp, 1)
        self.assertEqual(retained.cumulative_counts, (3, 7, 10))

        # The next good sample settles across the rejected sample.
        good = make_sample("p1", "start-a", 3, (10, 20, INF), (6, 11, 15))
        result = converter.submit(good)
        self.assertIsInstance(result, Settlement)
        self.assertEqual(result.baseline_timestamp, 1)
        self.assertEqual(
            [item.new_requests for item in result.increments], [3, 1, 1]
        )
        self.assertEqual(result.total_new_requests, 5)
        self.assertEqual(converter.baseline_for("p1").timestamp, 3)

    def test_negative_first_interval_is_rejected(self):
        converter = HistogramDeltaConverter()
        converter.submit(
            make_sample("p1", "start-a", 1, (10, 20, INF), (3, 7, 10))
        )
        with self.assertRaises(NegativeIncrementError) as caught:
            converter.submit(
                make_sample("p1", "start-a", 2, (10, 20, INF), (2, 8, 11))
            )
        error = caught.exception
        self.assertEqual((error.lower_bound, error.upper_bound), (0.0, 10))
        self.assertEqual(error.interval_increment, -1)
        self.assertEqual(converter.baseline_for("p1").timestamp, 1)

    def test_negative_delta_on_shared_bounds_after_boundary_change(self):
        converter = HistogramDeltaConverter()
        converter.submit(
            make_sample("p1", "start-a", 1, (10, 20, INF), (3, 7, 10))
        )
        # Shared bounds are 20 and +inf; the cumulative count at 20 drops.
        with self.assertRaises(NegativeIncrementError):
            converter.submit(
                make_sample("p1", "start-a", 2, (15, 20, INF), (4, 6, 12))
            )
        self.assertEqual(converter.baseline_for("p1").timestamp, 1)


class TestRetransmissions(unittest.TestCase):
    def test_identical_same_timestamp_retransmission_is_ignored(self):
        converter = HistogramDeltaConverter()
        original = make_sample("p1", "start-a", 1, (10, INF), (3, 10))
        converter.submit(original)

        result = converter.submit(
            make_sample("p1", "start-a", 1, (10, INF), (3, 10))
        )
        self.assertIsInstance(result, Retransmission)
        self.assertTrue(result.ignored)
        # Baseline untouched.
        self.assertEqual(converter.baseline_for("p1").timestamp, 1)

        # Settlement still works afterwards.
        settled = converter.submit(
            make_sample("p1", "start-a", 2, (10, INF), (4, 12))
        )
        self.assertIsInstance(settled, Settlement)
        self.assertEqual(
            [item.new_requests for item in settled.increments], [1, 2]
        )

    def test_conflicting_same_timestamp_retransmission_raises(self):
        converter = HistogramDeltaConverter()
        converter.submit(
            make_sample("p1", "start-a", 1, (10, INF), (3, 10))
        )
        with self.assertRaises(ConflictingRetransmissionError) as caught:
            converter.submit(
                make_sample("p1", "start-a", 1, (10, INF), (4, 10))
            )
        self.assertEqual(caught.exception.timestamp, 1)

        # A conflicting payload must not replace the baseline.
        self.assertEqual(
            converter.baseline_for("p1").cumulative_counts, (3, 10)
        )

        # The next good sample still settles against the original baseline.
        settled = converter.submit(
            make_sample("p1", "start-a", 2, (10, INF), (5, 13))
        )
        self.assertIsInstance(settled, Settlement)
        self.assertEqual(
            [item.new_requests for item in settled.increments], [2, 3]
        )

    def test_conflicting_retransmission_with_different_bounds_raises(self):
        converter = HistogramDeltaConverter()
        converter.submit(
            make_sample("p1", "start-a", 1, (10, INF), (3, 10))
        )
        with self.assertRaises(ConflictingRetransmissionError):
            converter.submit(
                make_sample("p1", "start-a", 1, (5, 10, INF), (1, 3, 10))
            )

    def test_identical_retransmission_after_rejected_sample_is_ignored(self):
        converter = HistogramDeltaConverter()
        converter.submit(
            make_sample("p1", "start-a", 1, (10, 20, INF), (3, 7, 10))
        )
        with self.assertRaises(NegativeIncrementError):
            converter.submit(
                make_sample("p1", "start-a", 2, (10, 20, INF), (5, 6, 12))
            )
        # Re-sending the retained baseline sample is a harmless retransmission.
        result = converter.submit(
            make_sample("p1", "start-a", 1, (10, 20, INF), (3, 7, 10))
        )
        self.assertIsInstance(result, Retransmission)


class TestProcessIdentityAndOrdering(unittest.TestCase):
    def test_restarted_process_first_sample_only_sets_baseline(self):
        converter = HistogramDeltaConverter()
        converter.submit(
            make_sample("p1", "start-a", 1, (10, INF), (3, 10))
        )
        restart = converter.submit(
            make_sample("p1", "start-b", 2, (10, INF), (1, 4))
        )
        self.assertIsInstance(restart, BaselineEstablished)
        self.assertEqual(restart.process_start, "start-b")

        # Settlement is against the restarted baseline, not the old one.
        settled = converter.submit(
            make_sample("p1", "start-b", 3, (10, INF), (2, 5))
        )
        self.assertEqual(
            [item.new_requests for item in settled.increments], [1, 1]
        )

    def test_out_of_order_timestamp_raises_and_keeps_baseline(self):
        converter = HistogramDeltaConverter()
        converter.submit(
            make_sample("p1", "start-a", 5, (10, INF), (3, 10))
        )
        with self.assertRaises(OutOfOrderTimestampError) as caught:
            converter.submit(
                make_sample("p1", "start-a", 4, (10, INF), (5, 12))
            )
        self.assertEqual(caught.exception.baseline_timestamp, 5)
        self.assertEqual(converter.baseline_for("p1").timestamp, 5)

    def test_multiple_processes_are_independent(self):
        converter = HistogramDeltaConverter()
        converter.submit(
            make_sample("p1", "start-a", 1, (10, INF), (1, 5))
        )
        converter.submit(
            make_sample("p2", "start-x", 1, (10, INF), (2, 8))
        )
        r1 = converter.submit(
            make_sample("p1", "start-a", 2, (10, INF), (3, 9))
        )
        r2 = converter.submit(
            make_sample("p2", "start-x", 2, (10, INF), (2, 10))
        )
        self.assertEqual(
            [item.new_requests for item in r1.increments], [2, 4]
        )
        self.assertEqual(
            [item.new_requests for item in r2.increments], [0, 2]
        )
        self.assertEqual(converter.tracked_processes(), ("p1", "p2"))


class TestPureSettlementFunction(unittest.TestCase):
    def test_compute_increments_directly(self):
        baseline = make_sample(
            "p1", "start-a", 1, (10, 20, INF), (3, 7, 10)
        )
        sample = make_sample(
            "p1", "start-a", 2, (15, 20, INF), (6, 9, 14)
        )
        result = compute_increments(baseline, sample)
        self.assertEqual(result.common_upper_bounds, (20, INF))
        self.assertEqual(
            [(item.lower_bound, item.upper_bound, item.new_requests)
             for item in result.increments],
            [(0.0, 20, 2), (20, INF, 2)],
        )

    def test_common_bounds_are_always_ascending(self):
        baseline = make_sample(
            "p1", "start-a", 1, (10, 20, 30, INF), (1, 2, 3, 4)
        )
        sample = make_sample(
            "p1", "start-a", 2, (5, 20, 40, INF), (1, 4, 5, 8)
        )
        result = compute_increments(baseline, sample)
        self.assertEqual(result.common_upper_bounds, (20, INF))


class TestSampleValidation(unittest.TestCase):
    def test_invalid_samples_are_rejected(self):
        invalid_cases = [
            # (kwargs, reason)
            (dict(process_id="", process_start="s", timestamp=1,
                  upper_bounds=(10, INF), cumulative_counts=(1, 2)),
             "empty process id"),
            (dict(process_id="p", process_start="", timestamp=1,
                  upper_bounds=(10, INF), cumulative_counts=(1, 2)),
             "empty process start"),
            (dict(process_id="p", process_start="s", timestamp="1",
                  upper_bounds=(10, INF), cumulative_counts=(1, 2)),
             "non-numeric timestamp"),
            (dict(process_id="p", process_start="s", timestamp=INF,
                  upper_bounds=(10, INF), cumulative_counts=(1, 2)),
             "infinite timestamp"),
            (dict(process_id="p", process_start="s", timestamp=1,
                  upper_bounds=(10, 20), cumulative_counts=(1, 2)),
             "last bound is not infinity"),
            (dict(process_id="p", process_start="s", timestamp=1,
                  upper_bounds=(20, 10, INF), cumulative_counts=(1, 2, 3)),
             "non-ascending bounds"),
            (dict(process_id="p", process_start="s", timestamp=1,
                  upper_bounds=(10, INF, 20), cumulative_counts=(1, 2, 3)),
             "infinity not last"),
            (dict(process_id="p", process_start="s", timestamp=1,
                  upper_bounds=(10, INF), cumulative_counts=(1,)),
             "length mismatch"),
            (dict(process_id="p", process_start="s", timestamp=1,
                  upper_bounds=(10, INF), cumulative_counts=(-1, 2)),
             "negative count"),
            (dict(process_id="p", process_start="s", timestamp=1,
                  upper_bounds=(10, 20, INF), cumulative_counts=(5, 3, 8)),
             "decreasing counts"),
            (dict(process_id="p", process_start="s", timestamp=1,
                  upper_bounds=(10, INF), cumulative_counts=(True, 2)),
             "boolean count"),
            (dict(process_id="p", process_start="s", timestamp=1,
                  upper_bounds=(True, INF), cumulative_counts=(1, 2)),
             "boolean bound"),
            (dict(process_id="p", process_start="s", timestamp=1,
                  upper_bounds=(), cumulative_counts=()),
             "empty bounds"),
        ]
        for kwargs, reason in invalid_cases:
            with self.subTest(reason=reason):
                with self.assertRaises(InvalidSampleError):
                    ProcessSample.build(**kwargs)

    def test_single_infinite_bucket_is_valid(self):
        sample = make_sample("p", "s", 1, (INF,), (7,))
        self.assertEqual(sample.upper_bounds, (INF,))

    def test_submitting_non_sample_object_is_rejected(self):
        converter = HistogramDeltaConverter()
        with self.assertRaises(InvalidSampleError):
            converter.submit(("p", "s", 1))


if __name__ == "__main__":
    unittest.main()
