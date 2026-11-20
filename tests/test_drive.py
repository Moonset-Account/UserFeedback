"""Segment-driven ingestion: mixed streams, interleaving, and crash-free retries."""

from __future__ import annotations

import unittest

from unique_index_builder import (
    Change,
    InvalidTransactionError,
    SnapshotSegment,
    UniqueIndexBuilder,
)

from .support import BuilderTestCase, tx


class SegmentDriveTests(BuilderTestCase):
    def test_drive_accepts_snapshot_and_transaction_segments_in_any_order(self) -> None:
        builder = UniqueIndexBuilder(watermark=2, expected_snapshot_batches=["b1", "b2"])

        drained = builder.drive(
            [
                tx(2, Change.insert(3, "c")),
                SnapshotSegment("b2", [(2, "b")]),
                tx(1, Change.update(1, "a2")),
                SnapshotSegment("b1", [(1, "a")]),
            ]
        )

        self.assertEqual([transaction.commit_seq for transaction in drained], [1, 2])
        builder.publish()
        self.assertIndexMatches(builder, {1: "a2", 2: "b", 3: "c"})

    def test_drive_reports_only_transactions_since_the_last_poll(self) -> None:
        builder = UniqueIndexBuilder(watermark=2, expected_snapshot_batches=["b1"])
        first = builder.drive([tx(1, Change.update(1, "a"))])
        second = builder.drive([tx(2, Change.update(1, "b"))])
        third = builder.drive([])

        self.assertEqual([transaction.commit_seq for transaction in first], [1])
        self.assertEqual([transaction.commit_seq for transaction in second], [2])
        self.assertEqual(third, ())

    def test_drive_rejects_unknown_segment_types(self) -> None:
        builder = UniqueIndexBuilder(watermark=1, expected_snapshot_batches=["b1"])
        with self.assertRaises(InvalidTransactionError):
            builder.drive([{"batch_id": "b1", "rows": []}])

    def test_out_of_order_segments_yield_the_same_index(self) -> None:
        segments = [
            SnapshotSegment("b0", [(1, "v1"), (2, "v2")]),
            SnapshotSegment("b1", [(3, "v3")]),
            tx(1, Change.update(1, "v1b")),
            tx(2, Change.delete(3)),
            tx(3, Change.insert(4, "v4")),
        ]

        left = UniqueIndexBuilder(watermark=3, expected_snapshot_batches=["b0", "b1"])
        left.drive(segments)
        left.publish()

        right = UniqueIndexBuilder(watermark=3, expected_snapshot_batches=["b1", "b0"])
        right.drive(list(reversed(segments)))
        right.publish()

        self.assertEqual(left.entries(), right.entries())
        self.assertIndexMatches(left, {1: "v1b", 2: "v2", 4: "v4"})

    def test_drive_segments_can_be_fed_one_at_a_time_between_polls(self) -> None:
        builder = UniqueIndexBuilder(watermark=1, expected_snapshot_batches=["b0"])
        builder.drive([SnapshotSegment("b0", [(1, "a"), (2, "b")])])
        builder.drive([tx(1, Change.update(1, "b"), Change.update(2, "a"))])
        builder.publish()

        self.assertEqual(builder.find_key("a"), 2)
        self.assertEqual(builder.find_key("b"), 1)


if __name__ == "__main__":
    unittest.main()
