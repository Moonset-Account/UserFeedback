"""Tests for the streaming median window operator.

Scenarios are grouped as requested by the task statement:

* normal behaviour (odd/even counts, duplicates, negative times/values)
* boundary behaviour (empty window, W = 0, exact window edge t - W,
  single-row-per-timestamp, partition switch)
* interleaved behaviour (batch splits inside a timestamp group, repeated
  values, many partitions, randomized cross-checks against a brute-force
  reference, and a check that resident state stays bounded).
"""

import random
import unittest

from median_window import (
    MedianWindow,
    OrderingError,
    WindowError,
    iter_medians,
    reference_output,
    run_stream,
)


def medians(records, window, seed=0):
    return [row.median for row in run_stream(records, window, seed=seed)]


class NormalScenarios(unittest.TestCase):
    def test_hand_computed_single_partition(self):
        # t=0: window [-2,0) empty -> NULL
        # t=1: [-1,1) = {10} -> 10   (both rows at t=1 agree)
        # t=3: [1,3) = {2,4} -> 3.0
        records = [
            ("p", 0, 10),
            ("p", 1, 2),
            ("p", 1, 4),
            ("p", 3, 8),
        ]
        self.assertEqual(medians(records, 2), [None, 10, 10, 3.0])

    def test_odd_count_returns_middle_value(self):
        records = [("p", 0, 5), ("p", 1, 1), ("p", 2, 9), ("p", 3, 100)]
        # t=3: [1,3) = {1,9} even -> 5.0; build a real odd case below
        out = medians(records, 2)
        self.assertEqual(out[3], 5.0)

        records = [("p", 0, 5), ("p", 0, 1), ("p", 1, 9), ("p", 2, 7)]
        # t=2: [0,2) = {5,1,9} -> 5
        self.assertEqual(medians(records, 2)[3], 5)

    def test_negative_times_and_values(self):
        records = [("p", -5, -4), ("p", -4, -1), ("p", -3, -10)]
        # t=-3: [-5,-3) = {-4,-1} -> -2.5
        self.assertEqual(medians(records, 2), [None, -4, -2.5])

    def test_float_values(self):
        records = [("p", 0, 1.5), ("p", 1, 2.5)]
        self.assertEqual(medians(records, 1), [None, 1.5])

    def test_duplicate_values_inside_window(self):
        records = [("p", 0, 4), ("p", 0, 4), ("p", 1, 4)]
        # t=1: [0,1) = {4,4} -> 4.0
        self.assertEqual(medians(records, 1), [None, None, 4.0])


class BoundaryScenarios(unittest.TestCase):
    def test_empty_window_returns_none(self):
        self.assertEqual(medians([("p", 7, 3)], 5), [None])

    def test_zero_window_always_empty(self):
        records = [("p", 0, 1), ("p", 1, 2), ("p", 1, 3), ("p", 2, 4)]
        self.assertEqual(medians(records, 0), [None] * 4)

    def test_window_edge_is_inclusive_of_t_minus_w(self):
        # W=3, t=4 -> [1,4) ; t=1 must be included, t=0 must not.
        records = [("p", 0, 1000), ("p", 1, 2), ("p", 4, 6)]
        # t=4: [1,4) = {2} -> 2
        self.assertEqual(medians(records, 3)[2], 2)

    def test_row_outside_edge_is_evicted(self):
        records = [("p", 0, 1000), ("p", 1, 2), ("p", 5, 6)]
        # t=5: [2,5) empty -> None
        self.assertEqual(medians(records, 3)[2], None)

    def test_all_rows_share_one_timestamp(self):
        records = [("p", 9, v) for v in (1, 2, 3, 4)]
        self.assertEqual(medians(records, 10), [None] * 4)

    def test_partition_resets_state(self):
        records = [
            ("a", 0, 10),
            ("a", 1, 20),
            ("b", 0, 100),
            ("b", 1, 200),
        ]
        out = medians(records, 5)
        # b must not see a's values.
        self.assertEqual(out, [None, 10, None, 100])

    def test_returning_to_closed_partition_is_rejected_in_strict_mode(self):
        op = MedianWindow(5, contiguous_partitions=True)
        op.push("a", 0, 1)
        op.push("b", 0, 2)
        with self.assertRaises(OrderingError):
            op.push("a", 1, 3)

    def test_partition_return_restarts_by_default(self):
        op = MedianWindow(5)
        op.push("a", 0, 1)
        op.push("b", 0, 2)
        row = op.push("a", 1, 3)
        # 'a' restarts fresh: no memory of its earlier row.
        self.assertIsNone(row.median)
        self.assertEqual(op.stats().partitions_seen, 3)

    def test_time_moving_backwards_is_rejected(self):
        op = MedianWindow(5)
        op.push("a", 5, 1)
        with self.assertRaises(OrderingError):
            op.push("a", 4, 2)

    def test_same_timestamp_after_advance_is_rejected(self):
        op = MedianWindow(5)
        op.push("a", 3, 1)
        op.push("a", 4, 2)
        with self.assertRaises(OrderingError):
            op.push("a", 3, 9)

    def test_invalid_window(self):
        for bad in (-1, 1.5, "2", True, None):
            with self.assertRaises(WindowError):
                MedianWindow(bad)

    def test_invalid_records(self):
        op = MedianWindow(2)
        with self.assertRaises(WindowError):
            op.push("p", 1.0, 5)
        with self.assertRaises(WindowError):
            op.push("p", 1, "5")
        with self.assertRaises(WindowError):
            op.push("p", 1, float("nan"))
        with self.assertRaises(WindowError):
            op.push("p", 1, True)
        with self.assertRaises(WindowError):
            op.push(None, 1, 5)
        with self.assertRaises(WindowError):
            op.push(["unhashable"], 1, 5)

    def test_null_values_are_kept_as_rows_but_not_counted(self):
        records = [("p", 0, None), ("p", 1, 5), ("p", 1, None), ("p", 2, 3)]
        # t=1: [0,1) = {NULL} -> no numeric values -> NULL
        # t=2: [0,2) = {NULL, 5, NULL} -> 5
        self.assertEqual(medians(records, 2), [None, None, None, 5])
        self.assertEqual(reference_output(records, 2), [None, None, None, 5])

    def test_all_null_window_is_empty(self):
        records = [("p", 0, None), ("p", 1, None)]
        self.assertEqual(medians(records, 5), [None, None])

    def test_malformed_records_rejected(self):
        op = MedianWindow(2)
        for bad in ["abc", ("p", 1), ("p", 1, 2, 3), 5, {"partition": "p"}]:
            with self.assertRaises(WindowError):
                op.push_many([bad])

    def test_push_after_finish_is_rejected(self):
        op = MedianWindow(1, retain_results=True)
        op.push("p", 0, 1)
        op.finish()
        with self.assertRaises(WindowError):
            op.push("p", 1, 2)
        self.assertEqual(len(op.finish()), 1)

    def test_results_are_optional_to_keep_memory_bounded(self):
        op = MedianWindow(2)
        rows = [op.push("p", t, t) for t in range(5)]
        self.assertEqual(op.results, ())
        self.assertEqual([r.row_number for r in rows], [1, 2, 3, 4, 5])
        keeping = MedianWindow(2, retain_results=True)
        keeping.push_many([("p", t, t) for t in range(5)])
        self.assertEqual(len(keeping.results), 5)


class InterleavedScenarios(unittest.TestCase):
    def test_batch_split_inside_timestamp_group_is_irrelevant(self):
        records = [
            ("p", 0, 4),
            ("p", 0, 4),
            ("p", 0, 9),
            ("p", 1, 1),
            ("p", 1, 1),
            ("p", 2, 7),
            ("p", 2, 8),
        ]
        whole = medians(records, 3)
        for cut in range(len(records) + 1):
            batches = [records[:cut], records[cut:]]
            op = MedianWindow(3)
            self.assertEqual(
                [r.median for r in op.push_batches(batches)], whole, f"cut={cut}"
            )
        # Also split into one batch per record.
        op = MedianWindow(3)
        out = [r.median for r in op.push_batches([[r] for r in records])]
        self.assertEqual(out, whole)

    def test_duplicates_at_shared_timestamp_with_split(self):
        records = [("p", 5, 2)] * 6 + [("p", 6, 2)] * 2 + [("p", 7, 2)] * 3
        whole = medians(records, 10)
        expected = [None] * 6 + [2.0] * 2 + [2.0] * 3
        self.assertEqual(whole, expected)
        for cut in (0, 1, 5, 6, 8, 9, 11):
            op = MedianWindow(10)
            out = [r.median for r in op.push_batches([records[:cut], records[cut:]])]
            self.assertEqual(out, whole, f"cut={cut}")

    def test_many_partitions_keep_order_and_numbers(self):
        records = [
            ("a", 0, 1),
            ("a", 2, 3),
            ("b", 0, 10),
            ("b", 5, 20),
            ("c", 1, 7),
        ]
        rows = run_stream(records, 4)
        self.assertEqual([r.row_number for r in rows], [1, 2, 3, 4, 5])
        self.assertEqual([r.partition for r in rows], ["a", "a", "b", "b", "c"])
        # a: t=2 sees [−2,1] = {1}; b: t=5 sees [1,4] (its t=0 row is out);
        # c: t=1 has no prior row.
        self.assertEqual(
            [r.median for r in rows], [None, 1, None, None, None]
        )

    def test_reference_cross_check_randomized(self):
        rng = random.Random(1234)
        for trial in range(200):
            window = rng.randint(0, 8)
            records = []
            for partition in ("a", "b", "c"):
                t = rng.randint(-5, 3)
                for _ in range(rng.randint(0, 12)):
                    t += rng.choice([0, 0, 1, 1, 2, 5])
                    records.append((partition, t, rng.randint(-20, 20)))
            expected = reference_output(records, window)
            for seed in (0, 9):
                got = medians(records, window, seed=seed)
                self.assertEqual(got, expected, f"trial={trial} seed={seed}")
            # Any batch split must agree too.
            cut = rng.randint(0, len(records))
            op = MedianWindow(window, seed=trial)
            got = [r.median for r in op.push_batches([records[:cut], records[cut:]])]
            self.assertEqual(got, expected, f"trial={trial} cut={cut}")

    def test_iter_medians_matches_run_stream(self):
        records = [("p", t, v) for t, v in enumerate([5, 3, 8, 1, 9])]
        self.assertEqual(
            list(iter_medians(records, 2)), medians(records, 2)
        )

    def test_resident_rows_bounded_by_window(self):
        """State must be O(rows in active window), not O(stream length)."""
        rng = random.Random(99)
        window = 4
        op = MedianWindow(window, seed=5)
        # 25 distinct timestamps, 3 rows each, values irrelevant.
        for t in range(25):
            for _ in range(3):
                op.push("p", t, rng.randint(0, 50))
        stats = op.stats()
        # Window [t-4, t] spans at most 5 distinct timestamps * 3 rows.
        self.assertLessEqual(stats.max_resident, (window + 1) * 3)
        self.assertLessEqual(stats.max_window_rows, (window + 1) * 3)
        self.assertLessEqual(stats.resident_rows, (window + 1) * 3)

    def test_resident_exactly_matches_window_rows_after_each_push(self):
        """No row outside ``[t-W, t]`` may still be resident."""
        rng = random.Random(77)
        window = 3
        op = MedianWindow(window, seed=8)
        held = []  # (time, value) currently in this partition
        next_partition = 0
        partition = "p0"
        for _ in range(300):
            if rng.random() < 0.1:
                # New partition: state must be fully released.
                held.clear()
                next_partition += 1
                partition = f"p{next_partition}"
                time = rng.randint(-3, 0)
            else:
                time = held[-1][0] + rng.choice([0, 1, 1, 2, 4]) if held else 0
            value = rng.randint(0, 9)
            op.push(partition, time, value)
            held = [(t, v) for (t, v) in held if time - window <= t <= time]
            held.append((time, value))
            self.assertEqual(
                op.stats().resident_rows,
                len(held),
                f"resident mismatch at time={time}",
            )

    def test_no_full_rebuilds_and_work_tracks_rows(self):
        rng = random.Random(2026)
        op = MedianWindow(8, seed=2)
        stamp = 0
        for _ in range(400):
            stamp += rng.choice([0, 1, 1, 1])
            op.push("p", stamp, rng.randint(0, 100))
        stats = op.stats()
        self.assertEqual(stats.rebuilds, 0)
        # One insert per row that ever entered the window, and no more
        # deletions than insertions.
        self.assertLessEqual(stats.insertions, stats.rows_in)
        self.assertLessEqual(stats.deletions, stats.insertions)
        # Rotations stay proportional to operations, i.e. sublinear in
        # window sorting work (a sort-based operator would need log n
        # comparisons per row rather than a bounded number of rotations).
        self.assertLessEqual(stats.rotations, 4 * stats.insertions)
        # At most two rank lookups per emitted row (one or two, zero if empty).
        self.assertGreater(stats.kth_queries, 0)
        self.assertLessEqual(stats.kth_queries, 2 * stats.rows_in)

    def test_single_value_present_then_absent(self):
        records = [("p", 0, 42), ("p", 1, 0), ("p", 2, 0), ("p", 3, 0)]
        # W=1 -> each row sees only the immediately preceding timestamp.
        self.assertEqual(medians(records, 1), [None, 42, 0, 0])

    def test_large_window_whole_stream(self):
        rng = random.Random(5)
        records = []
        for t in range(60):
            for _ in range(2):
                records.append(("p", t, rng.randint(0, 9)))
        expected = reference_output(records, 1000)
        self.assertEqual(medians(records, 1000), expected)


if __name__ == "__main__":
    unittest.main()
