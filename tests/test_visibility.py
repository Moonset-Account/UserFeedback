"""What a reader sees after publish: no stale values, no partial index."""

from __future__ import annotations

import unittest

from unique_index_builder import Change, UniqueIndexBuilder

from .support import BuilderTestCase, tx


class PublishedVisibilityTests(BuilderTestCase):
    def test_old_value_of_an_updated_row_is_gone(self) -> None:
        builder = UniqueIndexBuilder(watermark=1, expected_snapshot_batches=["b1"])
        builder.add_snapshot_batch("b1", [(1, "old"), (2, "other")])
        builder.apply_transaction(tx(1, Change.update(1, "new")))
        builder.publish()

        self.assertEqual(builder.find_key("old"), None)
        self.assertEqual(builder.find_key("new"), 1)
        self.assertEqual(builder.lookup(1), "new")

    def test_old_value_of_a_deleted_row_is_gone(self) -> None:
        builder = UniqueIndexBuilder(watermark=1, expected_snapshot_batches=["b1"])
        builder.add_snapshot_batch("b1", [(1, "old"), (2, "other")])
        builder.apply_transaction(tx(1, Change.delete(1)))
        builder.publish()

        self.assertIsNone(builder.lookup(1))
        self.assertIsNone(builder.find_key("old"))
        self.assertEqual(sorted(builder.entries()), ["other"])

    def test_value_released_by_one_row_can_be_reused_by_another(self) -> None:
        builder = UniqueIndexBuilder(watermark=2, expected_snapshot_batches=["b1"])
        builder.add_snapshot_batch("b1", [(1, "a"), (2, "b")])
        builder.apply_transaction(tx(1, Change.update(1, "c")))
        builder.apply_transaction(tx(2, Change.update(2, "a")))
        builder.publish()

        self.assertIndexMatches(builder, {1: "c", 2: "a"})
        self.assertIsNone(builder.find_key("b"))

    def test_swap_result_has_no_duplicate_and_both_new_values_resolve(self) -> None:
        builder = UniqueIndexBuilder(watermark=1, expected_snapshot_batches=["b1"])
        builder.add_snapshot_batch("b1", [(1, "a"), (2, "b")])
        builder.apply_transaction(tx(1, Change.update(1, "b"), Change.update(2, "a")))
        builder.publish()

        self.assertEqual(builder.find_key("a"), 2)
        self.assertEqual(builder.find_key("b"), 1)

    def test_entries_returns_a_copy_that_cannot_corrupt_the_index(self) -> None:
        builder = UniqueIndexBuilder(watermark=0, expected_snapshot_batches=["b1"])
        builder.add_snapshot_batch("b1", [(1, "a")])
        builder.publish()

        snapshot = builder.entries()
        snapshot["a"] = 999
        snapshot["injected"] = 1

        self.assertEqual(builder.find_key("a"), 1)
        self.assertIsNone(builder.find_key("injected"))
        self.assertEqual(len(builder), 1)

    def test_no_value_is_reachable_under_two_keys_after_publish(self) -> None:
        builder = UniqueIndexBuilder(watermark=3, expected_snapshot_batches=["b1", "b2"])
        builder.add_snapshot_batch("b1", [(1, "v1"), (2, "v2")])
        # A temporary collision that commit 2 later repairs.
        builder.apply_transaction(tx(1, Change.update(1, "v2")))
        builder.add_snapshot_batch("b2", [(3, "v3")])
        builder.apply_transaction(tx(2, Change.update(2, "v4")))
        builder.apply_transaction(tx(3, Change.update(1, "v1")))
        builder.publish()

        values = list(builder.entries())
        self.assertEqual(len(values), len(set(values)))
        for value in values:
            self.assertIsNotNone(builder.find_key(value))

    def test_membership_and_iteration_reflect_the_published_state(self) -> None:
        builder = UniqueIndexBuilder(watermark=1, expected_snapshot_batches=["b1"])
        builder.add_snapshot_batch("b1", [(1, "kept"), (2, "dropped")])
        builder.apply_transaction(tx(1, Change.delete(2)))
        builder.publish()

        self.assertIn("kept", builder)
        self.assertNotIn("dropped", builder)
        self.assertEqual(sorted(builder), ["kept"])


if __name__ == "__main__":
    unittest.main()
