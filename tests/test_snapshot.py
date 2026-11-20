"""Snapshot ingestion: arbitrary batch order, late batches, retries, overlaps."""

from __future__ import annotations

import unittest

from unique_index_builder import (
    Change,
    DuplicateSnapshotRowError,
    InvalidSnapshotRowError,
    SnapshotBatchConflictError,
    SnapshotRow,
    UnknownSnapshotBatchError,
    UniqueIndexBuilder,
)

from .support import BuilderTestCase, seed_builder, tx


class SnapshotIngestionTests(BuilderTestCase):
    def test_batches_may_arrive_in_any_order(self) -> None:
        batches = {
            "b1": [(1, "a"), (2, "b")],
            "b2": [(3, "c")],
            "b3": [(4, "d"), (5, "e")],
        }
        forward = seed_builder([("b1", batches["b1"]), ("b2", batches["b2"]), ("b3", batches["b3"])])
        reverse = seed_builder([("b3", batches["b3"]), ("b2", batches["b2"]), ("b1", batches["b1"])])

        forward.publish()
        reverse.publish()
        self.assertIndexMatches(reverse, {1: "a", 2: "b", 3: "c", 4: "d", 5: "e"})
        self.assertEqual(forward.entries(), reverse.entries())

    def test_snapshot_row_objects_and_pairs_are_equivalent(self) -> None:
        builder = UniqueIndexBuilder(watermark=0, expected_snapshot_batches=["b1"])
        builder.add_snapshot_batch("b1", [SnapshotRow(1, "a"), (2, "b"), [3, "c"]])
        builder.publish()
        self.assertIndexMatches(builder, {1: "a", 2: "b", 3: "c"})

    def test_identical_batch_redelivery_is_a_noop(self) -> None:
        builder = UniqueIndexBuilder(watermark=0, expected_snapshot_batches=["b1", "b2"])
        builder.add_snapshot_batch("b1", [(1, "a")])
        builder.add_snapshot_batch("b1", [(1, "a")])
        self.assertEqual(builder.arrived_snapshot_batches, ("b1",))
        self.assertEqual(builder.missing_snapshot_batches, ("b2",))

    def test_batch_redelivery_with_different_rows_is_rejected(self) -> None:
        builder = UniqueIndexBuilder(watermark=0, expected_snapshot_batches=["b1"])
        builder.add_snapshot_batch("b1", [(1, "a")])
        with self.assertRaises(SnapshotBatchConflictError):
            builder.add_snapshot_batch("b1", [(1, "a"), (2, "b")])

    def test_undeclared_batch_id_is_rejected(self) -> None:
        builder = UniqueIndexBuilder(watermark=0, expected_snapshot_batches=["b1"])
        with self.assertRaises(UnknownSnapshotBatchError):
            builder.add_snapshot_batch("b9", [(1, "a")])

    def test_primary_key_spanning_two_batches_is_rejected(self) -> None:
        builder = UniqueIndexBuilder(watermark=0, expected_snapshot_batches=["b1", "b2"])
        builder.add_snapshot_batch("b1", [(1, "a")])
        with self.assertRaises(DuplicateSnapshotRowError):
            builder.add_snapshot_batch("b2", [(1, "a")])

    def test_primary_key_repeated_inside_one_batch_is_rejected(self) -> None:
        builder = UniqueIndexBuilder(watermark=0, expected_snapshot_batches=["b1"])
        with self.assertRaises(DuplicateSnapshotRowError):
            builder.add_snapshot_batch("b1", [(1, "a"), (1, "b")])

    def test_duplicate_values_inside_snapshot_are_allowed_until_publish(self) -> None:
        builder = UniqueIndexBuilder(watermark=0, expected_snapshot_batches=["b1"])
        builder.add_snapshot_batch("b1", [(1, "same"), (2, "same")])
        report = builder.check_publish()
        self.assertFalse(report.published)
        self.assertEqual(report.conflicts[0].primary_keys, (1, 2))
        self.assertFalse(report.evaluated_on_partial_state)

    def test_malformed_rows_are_rejected(self) -> None:
        builder = UniqueIndexBuilder(watermark=0, expected_snapshot_batches=["b1"])
        for bad in [(1,), (1, "a", "extra"), "not-a-row", 42, (None, "a"), (1, None)]:
            with self.subTest(row=bad):
                with self.assertRaises(InvalidSnapshotRowError):
                    builder.add_snapshot_batch("b1", [bad])

    def test_unhashable_rows_are_rejected(self) -> None:
        builder = UniqueIndexBuilder(watermark=0, expected_snapshot_batches=["b1"])
        with self.assertRaises(InvalidSnapshotRowError):
            builder.add_snapshot_batch("b1", [([1, 2], "a")])

    def test_late_snapshot_batch_does_not_undo_a_newer_update(self) -> None:
        # The change for key 1 arrives before batch b1 that contains key 1.
        builder = UniqueIndexBuilder(watermark=3, expected_snapshot_batches=["b1", "b2"])
        builder.apply_transaction(tx(1, Change.update(1, "new")))
        builder.apply_transaction(tx(2, Change.insert(3, "c")))
        builder.apply_transaction(tx(3, Change.delete(2)))

        builder.add_snapshot_batch("b2", [(2, "old-2"), (3, "old-3")])
        builder.add_snapshot_batch("b1", [(1, "old-1")])

        builder.publish()
        self.assertIndexMatches(builder, {1: "new", 3: "c"})

    def test_snapshot_only_batch_arrival_order_is_invisible(self) -> None:
        rows = [(index, "v{}".format(index)) for index in range(50)]
        batches = [
            ("b0", rows[:10]),
            ("b1", rows[10:20]),
            ("b2", rows[20:]),
        ]
        left = seed_builder(batches)
        right = seed_builder(list(reversed(batches)))
        left.publish()
        right.publish()
        self.assertEqual(left.entries(), right.entries())

    def test_empty_manifest_is_a_valid_empty_base(self) -> None:
        builder = UniqueIndexBuilder(watermark=0, expected_snapshot_batches=[])
        builder.publish()
        self.assertEqual(builder.entries(), {})
        self.assertEqual(len(builder), 0)

    def test_heterogeneous_key_and_batch_id_types_are_supported(self) -> None:
        # Hashable values need not be mutually comparable (int vs str), so the
        # builder must never rely on a bare ``sorted`` over raw values.
        builder = UniqueIndexBuilder(
            watermark=1, expected_snapshot_batches=["b-str", 2, ("b", 3)]
        )
        builder.add_snapshot_batch(("b", 3), [("k-str", "v-str")])
        builder.add_snapshot_batch(2, [(7, "v-int"), (8, 99)])
        builder.add_snapshot_batch("b-str", [(True, "v-bool")])
        builder.apply_transaction(tx(1, Change.update("k-str", "v-new")))
        builder.publish()

        self.assertEqual(builder.find_key("v-int"), 7)
        self.assertEqual(builder.find_key(99), 8)
        self.assertEqual(builder.find_key("v-bool"), True)
        self.assertEqual(builder.find_key("v-new"), "k-str")
        self.assertEqual(builder.missing_snapshot_batches, ())


if __name__ == "__main__":
    unittest.main()
