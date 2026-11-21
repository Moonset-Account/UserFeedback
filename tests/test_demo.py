"""Docstring examples and end-to-end demonstration tests.

These exercise the documented examples through the *real* operator (no
pre-baked expected output): every assertion below is compared against either a
hand-computed value or the brute-force reference in
``median_window.reference_output``.
"""

import doctest
import unittest

import median_window.treap
import median_window.window
from median_window import MedianWindow, reference_output, run_stream


def load_tests(loader, tests, ignore):  # noqa: ARG001 - unittest protocol
    tests.addTests(doctest.DocTestSuite(median_window.window))
    tests.addTests(doctest.DocTestSuite(median_window.treap))
    return tests


class Demonstration(unittest.TestCase):
    """A readable end-to-end walk-through used by the README."""

    def test_worked_example(self):
        records = [
            ("sensor-a", 0, 10),
            ("sensor-a", 1, 4),
            ("sensor-a", 1, 6),
            ("sensor-a", 2, 9),
            ("sensor-a", 2, 1),
            ("sensor-b", 0, 100),
            ("sensor-b", 1, 200),
        ]
        rows = run_stream(records, window=2)

        got = [(r.row_number, r.partition, r.time, r.median) for r in rows]
        expected_medians = reference_output(records, 2)
        self.assertEqual([r.median for r in rows], expected_medians)
        # sensor-a, t=2: [0,2) = {10,4,6} -> 6
        # sensor-b, t=1: [−1,1) = {100} -> 100
        self.assertEqual(
            got,
            [
                (1, "sensor-a", 0, None),
                (2, "sensor-a", 1, 10),
                (3, "sensor-a", 1, 10),
                (4, "sensor-a", 2, 6),
                (5, "sensor-a", 2, 6),
                (6, "sensor-b", 0, None),
                (7, "sensor-b", 1, 100),
            ],
        )

    def test_worked_example_split_into_streaming_batches(self):
        records = [
            ("sensor-a", 0, 10),
            ("sensor-a", 1, 4),
            ("sensor-a", 1, 6),
            ("sensor-a", 2, 9),
            ("sensor-a", 2, 1),
            ("sensor-b", 0, 100),
            ("sensor-b", 1, 200),
        ]
        whole = [r.median for r in run_stream(records, 2)]
        op = MedianWindow(2)
        streamed = [r.median for r in op.push_batches([[r] for r in records])]
        self.assertEqual(streamed, whole)

    def test_stats_are_reported(self):
        op = MedianWindow(3, seed=11)
        for t, v in enumerate(range(10, 30)):
            op.push("p", t, v)
        stats = op.stats()
        self.assertEqual(stats.rows_in, 20)
        self.assertEqual(stats.rows_out, 20)
        self.assertEqual(stats.rebuilds, 0)
        self.assertGreater(stats.insertions, 0)
        self.assertGreater(stats.deletions, 0)  # evictions happened
        self.assertGreater(stats.rotations, 0)  # treap rebalanced
        self.assertGreaterEqual(stats.kth_queries, stats.rows_out)


if __name__ == "__main__":
    unittest.main()
