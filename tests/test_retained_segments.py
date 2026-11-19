"""Retained-instant evidence: distinct-source votes and closed intervals."""

from __future__ import annotations

import unittest

from handshake_time import TimeReceipt, UtcInterval, retained_segments

from tests.fixtures import utc


class RetainedSegmentsTests(unittest.TestCase):
    def test_single_source_meets_threshold_of_one(self) -> None:
        from tests.fixtures import receipt

        segments = retained_segments([receipt(0, 10, "gps")], min_sources=1)
        self.assertEqual(segments, (UtcInterval(utc(0), utc(10)),))

    def test_copy_of_same_source_counts_once(self) -> None:
        """Three copies of one source must not satisfy min_sources=2."""

        from tests.fixtures import receipt

        receipts = [receipt(0, 10, "gps") for _ in range(3)]
        self.assertEqual(retained_segments(receipts, min_sources=2), ())

    def test_two_same_source_fragments_still_one_vote(self) -> None:
        """Unioning one source's own intervals must not fabricate a second vote."""

        from tests.fixtures import receipt

        receipts = [receipt(0, 5, "gps"), receipt(5, 10, "gps")]
        self.assertEqual(retained_segments(receipts, min_sources=2), ())

    def test_same_source_copies_combine_with_one_other_source(self) -> None:
        from tests.fixtures import receipt

        receipts = [receipt(0, 10, "gps"), receipt(0, 10, "gps"), receipt(0, 10, "ntp")]
        segments = retained_segments(receipts, min_sources=2)
        self.assertEqual(segments, (UtcInterval(utc(0), utc(10)),))

    def test_two_distinct_sources_retain_their_overlap(self) -> None:
        from tests.fixtures import receipt

        receipts = [receipt(0, 10, "gps"), receipt(5, 15, "ntp")]
        segments = retained_segments(receipts, min_sources=2)
        self.assertEqual(segments, (UtcInterval(utc(5), utc(10)),))

    def test_three_sources_intersect_at_threshold_two(self) -> None:
        """Coverage 2.. needs 5..15 here; 9..10 has three votes but stays merged."""

        from tests.fixtures import receipt

        receipts = [
            receipt(0, 10, "gps"),
            receipt(5, 15, "ntp"),
            receipt(9, 20, "radio"),
        ]
        segments = retained_segments(receipts, min_sources=2)
        self.assertEqual(segments, (UtcInterval(utc(5), utc(15)),))
        # Raising the threshold to three splits the evidence into 9..10 only.
        self.assertEqual(
            retained_segments(receipts, min_sources=3),
            (UtcInterval(utc(9), utc(10)),),
        )
        self.assertEqual(retained_segments(receipts, min_sources=4), ())

    def test_segments_separated_by_gap_stay_separated(self) -> None:
        from tests.fixtures import receipt

        receipts = [
            receipt(0, 4, "gps"),
            receipt(0, 4, "ntp"),
            receipt(6, 10, "gps"),
            receipt(6, 10, "radio"),
        ]
        segments = retained_segments(receipts, min_sources=2)
        self.assertEqual(
            segments,
            (UtcInterval(utc(0), utc(4)), UtcInterval(utc(6), utc(10))),
        )

    def test_touching_segments_merge_at_closed_endpoint(self) -> None:
        """Closed intervals that share an endpoint form one continuous segment."""

        from tests.fixtures import receipt

        receipts = [receipt(0, 5, "gps"), receipt(5, 10, "ntp")]
        segments = retained_segments(receipts, min_sources=2)
        self.assertEqual(segments, (UtcInterval(utc(5), utc(5)),))

    def test_isolated_instant_supported_by_two_sources(self) -> None:
        """A single shared endpoint is retained even with no shared slab."""

        from tests.fixtures import receipt

        receipts = [receipt(0, 10, "gps"), receipt(10, 20, "ntp")]
        segments = retained_segments(receipts, min_sources=2)
        self.assertEqual(segments, (UtcInterval(utc(10), utc(10)),))

    def test_unauthenticated_receipts_are_ignored(self) -> None:
        from tests.fixtures import receipt

        receipts = [receipt(0, 10, "gps"), receipt(0, 10, "rogue", authenticated=False)]
        self.assertEqual(retained_segments(receipts, min_sources=2), ())
        self.assertEqual(
            retained_segments(receipts, min_sources=1),
            (UtcInterval(utc(0), utc(10)),),
        )

    def test_interleaved_partial_votes_retain_only_qualifying_pieces(self) -> None:
        """Staircase: A 0-6, B 2-8, C 4-10 at threshold 2."""

        from tests.fixtures import receipt

        receipts = [
            receipt(0, 6, "gps"),
            receipt(2, 8, "ntp"),
            receipt(4, 10, "radio"),
        ]
        segments = retained_segments(receipts, min_sources=2)
        self.assertEqual(segments, (UtcInterval(utc(2), utc(8)),))

    def test_no_receipts_yield_no_evidence(self) -> None:
        self.assertEqual(retained_segments([], min_sources=1), ())

    def test_min_sources_validation(self) -> None:
        with self.assertRaises(ValueError):
            retained_segments([], min_sources=0)
        with self.assertRaises(TypeError):
            retained_segments([], min_sources=True)  # type: ignore[arg-type]


class ReceiptValidationTests(unittest.TestCase):
    def test_reversed_interval_is_rejected(self) -> None:
        from datetime import timezone

        from tests.fixtures import utc

        with self.assertRaises(ValueError):
            TimeReceipt(start=utc(10), end=utc(0), source="gps", authenticated=True)

    def test_naive_datetime_is_rejected(self) -> None:
        from datetime import datetime

        with self.assertRaises(ValueError):
            TimeReceipt(
                start=datetime(2026, 11, 20),
                end=datetime(2026, 11, 20, 1),
                source="gps",
            )

    def test_empty_source_is_rejected(self) -> None:
        from tests.fixtures import utc

        with self.assertRaises(ValueError):
            TimeReceipt(start=utc(0), end=utc(1), source="   ")

    def test_source_is_normalized(self) -> None:
        from tests.fixtures import utc

        receipt = TimeReceipt(start=utc(0), end=utc(1), source="  gps  ")
        self.assertEqual(receipt.source, "gps")

    def test_interval_rejects_naive_bounds(self) -> None:
        from datetime import datetime

        with self.assertRaises(ValueError):
            UtcInterval(start=datetime(2026, 11, 20), end=datetime(2026, 11, 20, 1))


if __name__ == "__main__":
    unittest.main()
