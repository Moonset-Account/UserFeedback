"""Publication gates: missing commits, missing batches, conflicts, retries."""

from __future__ import annotations

import unittest

from unique_index_builder import (
    BuilderSealedError,
    Change,
    ConfigurationError,
    IndexNotPublishedError,
    PublishBlocked,
    UniqueIndexBuilder,
)

from .support import BuilderTestCase, blocked_report, seed_builder, tx


class PublishPreconditionTests(BuilderTestCase):
    def test_publish_succeeds_when_everything_is_present_and_unique(self) -> None:
        builder = seed_builder(
            [("b1", [(1, "a"), (2, "b")])],
            [tx(1, Change.update(1, "a")), tx(2, Change.insert(3, "c"))],
            watermark=2,
        )
        report = builder.publish()
        self.assertTrue(report.published)
        self.assertEqual(report.watermark, 2)
        self.assertIndexMatches(builder, {1: "a", 2: "b", 3: "c"})

    def test_lookup_before_publish_raises(self) -> None:
        builder = UniqueIndexBuilder(watermark=1, expected_snapshot_batches=["b1"])
        with self.assertRaises(IndexNotPublishedError):
            builder.lookup(1)
        with self.assertRaises(IndexNotPublishedError):
            builder.find_key("a")
        with self.assertRaises(IndexNotPublishedError):
            builder.entries()

    def test_missing_commit_numbers_block_publish(self) -> None:
        builder = seed_builder(
            [("b1", [(1, "a")])],
            [tx(1, Change.update(1, "a")), tx(3, Change.insert(4, "d"))],
            watermark=3,
        )
        report = blocked_report(builder)
        self.assertFalse(report.published)
        self.assertEqual(report.missing_commit_seqs, (2,))
        self.assertFalse(report.has_conflicts)

    def test_missing_snapshot_batch_blocks_publish_even_with_full_log(self) -> None:
        builder = UniqueIndexBuilder(watermark=2, expected_snapshot_batches=["b1", "b2"])
        builder.add_snapshot_batch("b1", [(1, "a")])
        builder.apply_transaction(tx(1, Change.update(1, "a")))
        builder.apply_transaction(tx(2, Change.insert(2, "b")))
        report = blocked_report(builder)
        self.assertFalse(report.snapshot_complete)
        self.assertEqual(report.missing_snapshot_batches, ("b2",))
        self.assertEqual(report.missing_commit_seqs, ())

    def test_missing_commits_are_reported_as_compressed_ranges(self) -> None:
        builder = seed_builder(
            [("b1", [(1, "a")])],
            [tx(1, Change.update(1, "a")), tx(2, Change.update(1, "a")), tx(6, Change.update(1, "a"))],
            watermark=6,
        )
        report = blocked_report(builder)
        self.assertEqual(report.missing_commit_seqs, (3, 4, 5))
        self.assertEqual(report.missing_commit_ranges, ((3, 5),))

    def test_conflict_lists_the_actual_primary_keys(self) -> None:
        builder = seed_builder(
            [("b1", [(1, "dup"), (2, "dup"), (3, "solo")])],
            [tx(1, Change.delete(3))],
            watermark=1,
        )
        report = blocked_report(builder)
        self.assertTrue(report.has_conflicts)
        self.assertFalse(report.has_missing_commits)
        self.assertEqual(report.conflicts[0].value, "dup")
        self.assertEqual(report.conflicts[0].primary_keys, (1, 2))
        self.assertIn("duplicate values", report.summary())
        self.assertIn("primary keys [1, 2]", report.summary())

    def test_missing_commits_and_conflicts_are_reported_separately(self) -> None:
        builder = seed_builder(
            [("b1", [(1, "dup"), (2, "dup")])],
            [tx(1, Change.update(5, "x"))],
            watermark=3,
        )
        report = blocked_report(builder)
        self.assertFalse(report.published)
        self.assertEqual(report.missing_commit_seqs, (2, 3))
        self.assertEqual(report.conflict_map, {"dup": (1, 2)})
        self.assertTrue(report.evaluated_on_partial_state)

    def test_conflict_is_provisional_while_a_commit_is_missing(self) -> None:
        builder = UniqueIndexBuilder(watermark=3, expected_snapshot_batches=["b1"])
        builder.add_snapshot_batch("b1", [(1, "dup"), (2, "dup")])
        builder.apply_transaction(tx(1, Change.update(1, "dup")))
        report = blocked_report(builder)
        self.assertEqual(report.missing_commit_seqs, (2, 3))
        # Commits 2 and 3 could still move one of the colliding rows, so the
        # conflict list is a hint, not a verdict, at this point.
        self.assertTrue(report.evaluated_on_partial_state)

    def test_blocked_publish_leaves_the_builder_unpublished_and_retryable(self) -> None:
        builder = seed_builder(
            [("b1", [(1, "dup"), (2, "dup")])],
            [],
            watermark=1,
        )
        report = blocked_report(builder)
        self.assertEqual(report.conflict_map, {"dup": (1, 2)})
        self.assertFalse(builder.published)
        self.assertIsNone(builder.published_at)

        # One commit both fills the missing commit number and repairs the
        # duplicate; the same watermark can then be published.
        builder.apply_transaction(tx(1, Change.update(2, "fixed")))
        builder.publish()
        self.assertTrue(builder.published)
        self.assertIndexMatches(builder, {1: "dup", 2: "fixed"})

    def test_missing_commit_can_be_supplied_and_publish_retried(self) -> None:
        builder = seed_builder(
            [("b1", [(1, "a")])],
            [tx(1, Change.update(1, "a")), tx(3, Change.insert(4, "d"))],
            watermark=3,
        )
        self.assertEqual(blocked_report(builder).missing_commit_seqs, (2,))

        builder.apply_transaction(tx(2, Change.insert(2, "b")))
        builder.publish()
        self.assertIndexMatches(builder, {1: "a", 2: "b", 4: "d"})

    def test_missing_snapshot_batch_can_be_supplied_and_publish_retried(self) -> None:
        builder = UniqueIndexBuilder(watermark=1, expected_snapshot_batches=["b1", "b2"])
        builder.add_snapshot_batch("b1", [(1, "a")])
        builder.apply_transaction(tx(1, Change.insert(2, "b")))
        self.assertEqual(blocked_report(builder).missing_snapshot_batches, ("b2",))

        builder.add_snapshot_batch("b2", [(3, "c")])
        builder.publish()
        self.assertIndexMatches(builder, {1: "a", 2: "b", 3: "c"})

    def test_repeated_blocked_publish_is_stable(self) -> None:
        builder = seed_builder([("b1", [(1, "a")])], [], watermark=5)
        first = blocked_report(builder)
        second = blocked_report(builder)
        self.assertEqual(first.missing_commit_seqs, second.missing_commit_seqs)
        self.assertEqual(first.missing_commit_seqs, (1, 2, 3, 4, 5))

    def test_publish_is_idempotent_after_success(self) -> None:
        builder = seed_builder([("b1", [(1, "a")])], [], watermark=0)
        first = builder.publish()
        second = builder.publish()
        self.assertTrue(first.published)
        self.assertTrue(second.published)
        self.assertEqual(builder.published_at, 0)

    def test_mutating_a_published_builder_is_rejected(self) -> None:
        builder = seed_builder([("b1", [(1, "a")])], [], watermark=0)
        builder.publish()
        with self.assertRaises(BuilderSealedError):
            builder.add_snapshot_batch("b1", [(1, "a")])
        with self.assertRaises(BuilderSealedError):
            builder.apply_transaction(tx(1, Change.update(1, "b")))

    def test_publish_below_configured_watermark_is_allowed(self) -> None:
        builder = seed_builder(
            [("b1", [(1, "a")])],
            [tx(1, Change.update(1, "b")), tx(2, Change.update(1, "c"))],
            watermark=5,
        )
        report = builder.publish(upto=1)
        self.assertEqual(report.watermark, 1)
        self.assertEqual(builder.lookup(1), "b")
        self.assertEqual(tuple(builder.covered_commit_seqs), (1,))

    def test_publish_above_the_configured_watermark_is_rejected(self) -> None:
        builder = seed_builder([("b1", [(1, "a")])], [], watermark=2)
        with self.assertRaises(ConfigurationError):
            builder.publish(upto=3)
        with self.assertRaises(ConfigurationError):
            builder.check_publish(upto=3)

    def test_invalid_configuration_is_rejected(self) -> None:
        with self.assertRaises(ConfigurationError):
            UniqueIndexBuilder(watermark="5", expected_snapshot_batches=["b1"])
        with self.assertRaises(ConfigurationError):
            UniqueIndexBuilder(watermark=1, expected_snapshot_batches=["b1", "b1"])
        with self.assertRaises(ConfigurationError):
            UniqueIndexBuilder(
                watermark=1, expected_snapshot_batches=["b1"], snapshot_floor=3
            )
        with self.assertRaises(ConfigurationError):
            UniqueIndexBuilder(watermark=1, expected_snapshot_batches="b1")

    def test_publish_blocked_error_carries_a_report(self) -> None:
        builder = seed_builder([("b1", [(1, "a")])], [], watermark=1)
        with self.assertRaises(PublishBlocked) as caught:
            builder.publish()
        self.assertEqual(caught.exception.report.missing_commit_seqs, (1,))
        self.assertIn("watermark 1", str(caught.exception))

    def test_try_publish_never_raises(self) -> None:
        builder = seed_builder([("b1", [(1, "a")])], [], watermark=1)
        report = builder.try_publish()
        self.assertFalse(report)
        self.assertTrue(report.blocked)


if __name__ == "__main__":
    unittest.main()
