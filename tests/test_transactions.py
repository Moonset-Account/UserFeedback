"""Transaction replay: ordering, post-images, swaps, buffering above the watermark."""

from __future__ import annotations

import unittest

from unique_index_builder import (
    Change,
    InvalidChangeError,
    InvalidTransactionError,
    StaleTransactionError,
    Transaction,
    TransactionConflictError,
    UniqueIndexBuilder,
)

from .support import BuilderTestCase, state_after, tx


class TransactionModelTests(unittest.TestCase):
    def test_delete_must_not_carry_a_value(self) -> None:
        with self.assertRaises(InvalidChangeError):
            Change(kind="delete", primary_key=1, value="unexpected")

    def test_insert_and_update_require_a_value(self) -> None:
        for kind in ("insert", "update"):
            with self.subTest(kind=kind):
                with self.assertRaises(InvalidChangeError):
                    Change(kind=kind, primary_key=1)

    def test_unknown_change_kind_is_rejected(self) -> None:
        with self.assertRaises(InvalidChangeError):
            Change(kind="upsert", primary_key=1, value="a")

    def test_none_primary_key_is_rejected(self) -> None:
        with self.assertRaises(InvalidChangeError):
            Change.insert(None, "a")

    def test_negative_or_non_integer_commit_seq_is_rejected(self) -> None:
        for seq in (-1, "1", 1.0, True):
            with self.subTest(seq=seq):
                with self.assertRaises(InvalidTransactionError):
                    Transaction(commit_seq=seq)

    def test_change_list_is_normalised_to_a_tuple(self) -> None:
        transaction = Transaction(commit_seq=1, changes=[Change.insert(1, "a")])
        self.assertIsInstance(transaction.changes, tuple)

    def test_non_change_entries_are_rejected(self) -> None:
        with self.assertRaises(InvalidTransactionError):
            Transaction(commit_seq=1, changes=[("insert", 1, "a")])


class TransactionReplayTests(BuilderTestCase):
    def test_commit_order_not_submission_order_decides_the_result(self) -> None:
        builder = UniqueIndexBuilder(watermark=2, expected_snapshot_batches=["b1"])
        builder.add_snapshot_batch("b1", [(1, "v0")])
        builder.apply_transaction(tx(2, Change.update(1, "v2")))
        builder.apply_transaction(tx(1, Change.update(1, "v1")))
        builder.publish()
        self.assertEqual(builder.lookup(1), "v2")

    def test_two_rows_in_one_transaction_swap_unique_values(self) -> None:
        # The swap is legal exactly because both changes share one commit: an
        # intermediate state would have a duplicate value, the committed state
        # does not.
        builder = UniqueIndexBuilder(watermark=1, expected_snapshot_batches=["b1"])
        builder.add_snapshot_batch("b1", [(1, "a"), (2, "b")])
        builder.apply_transaction(
            tx(1, Change.update(1, "b"), Change.update(2, "a"))
        )
        builder.publish()
        self.assertIndexMatches(builder, {1: "b", 2: "a"})

    def test_swap_inside_one_transaction_does_not_report_a_conflict(self) -> None:
        builder = UniqueIndexBuilder(watermark=1, expected_snapshot_batches=["b1"])
        builder.add_snapshot_batch("b1", [(1, "a"), (2, "b")])
        builder.apply_transaction(tx(1, Change.update(1, "b"), Change.update(2, "a")))
        report = builder.check_publish()
        self.assertTrue(report.published)
        self.assertEqual(report.conflicts, ())

    def test_intermediate_duplicate_across_two_commits_is_a_conflict(self) -> None:
        builder = UniqueIndexBuilder(watermark=2, expected_snapshot_batches=["b1"])
        builder.add_snapshot_batch("b1", [(1, "a"), (2, "b"), (3, "c")])
        builder.apply_transaction(tx(1, Change.update(1, "b")))
        # Commit 2 touches an unrelated row, so the collision introduced by
        # commit 1 is still live at the watermark and must block publication.
        builder.apply_transaction(tx(2, Change.update(3, "c2")))
        report = builder.check_publish()
        self.assertFalse(report.published)
        self.assertEqual(report.conflict_map, {"b": (1, 2)})

    def test_chained_updates_collapse_to_the_last_value(self) -> None:
        builder = UniqueIndexBuilder(watermark=3, expected_snapshot_batches=["b1"])
        builder.add_snapshot_batch("b1", [(1, "v0")])
        for seq, value in ((1, "v1"), (2, "v2"), (3, "v3")):
            builder.apply_transaction(tx(seq, Change.update(1, value)))
        builder.publish()
        self.assertIndexMatches(builder, {1: "v3"})
        self.assertIsNone(builder.find_key("v0"))
        self.assertIsNone(builder.find_key("v1"))

    def test_repeated_writes_to_one_key_inside_a_transaction_keep_the_last(self) -> None:
        builder = UniqueIndexBuilder(watermark=1, expected_snapshot_batches=["b1"])
        builder.add_snapshot_batch("b1", [(1, "v0")])
        builder.apply_transaction(
            tx(
                1,
                Change.update(1, "v1"),
                Change.update(1, "v2"),
                Change.update(1, "v3"),
            )
        )
        builder.publish()
        self.assertIndexMatches(builder, {1: "v3"})

    def test_delete_after_update_inside_one_transaction_wins(self) -> None:
        builder = UniqueIndexBuilder(watermark=1, expected_snapshot_batches=["b1"])
        builder.add_snapshot_batch("b1", [(1, "v0")])
        builder.apply_transaction(tx(1, Change.update(1, "v1"), Change.delete(1)))
        builder.publish()
        self.assertEqual(builder.entries(), {})

    def test_delete_then_reinsert_under_a_new_value(self) -> None:
        builder = UniqueIndexBuilder(watermark=2, expected_snapshot_batches=["b1"])
        builder.add_snapshot_batch("b1", [(1, "old")])
        builder.apply_transaction(tx(1, Change.delete(1)))
        builder.apply_transaction(tx(2, Change.insert(1, "new")))
        builder.publish()
        self.assertIndexMatches(builder, {1: "new"})
        self.assertIsNone(builder.find_key("old"))

    def test_insert_of_a_key_absent_from_the_snapshot(self) -> None:
        builder = UniqueIndexBuilder(watermark=1, expected_snapshot_batches=["b1"])
        builder.add_snapshot_batch("b1", [(1, "a")])
        builder.apply_transaction(tx(1, Change.insert(99, "z")))
        builder.publish()
        self.assertIndexMatches(builder, {1: "a", 99: "z"})

    def test_deleting_an_unknown_key_is_a_noop(self) -> None:
        builder = UniqueIndexBuilder(watermark=1, expected_snapshot_batches=["b1"])
        builder.add_snapshot_batch("b1", [(1, "a")])
        builder.apply_transaction(tx(1, Change.delete(404)))
        builder.publish()
        self.assertIndexMatches(builder, {1: "a"})

    def test_transactions_above_the_watermark_are_ignored(self) -> None:
        builder = UniqueIndexBuilder(watermark=2, expected_snapshot_batches=["b1"])
        builder.add_snapshot_batch("b1", [(1, "v0")])
        builder.apply_transaction(tx(1, Change.update(1, "v1")))
        builder.apply_transaction(tx(2, Change.update(1, "v2")))
        builder.apply_transaction(tx(3, Change.update(1, "v3")))
        builder.apply_transaction(tx(4, Change.update(1, "v4")))
        builder.publish()
        self.assertEqual(builder.lookup(1), "v2")
        self.assertEqual(tuple(builder.covered_commit_seqs), (1, 2))

    def test_queued_transactions_above_the_watermark_are_still_visible_via_poll(self) -> None:
        builder = UniqueIndexBuilder(watermark=1, expected_snapshot_batches=["b1"])
        builder.apply_transaction(tx(2, Change.update(1, "later")))
        drained = builder.poll_transactions()
        self.assertEqual([transaction.commit_seq for transaction in drained], [2])
        self.assertEqual(builder.poll_transactions(), ())

    def test_identical_transaction_resubmission_is_a_noop(self) -> None:
        builder = UniqueIndexBuilder(watermark=2, expected_snapshot_batches=["b1"])
        transaction = tx(1, Change.update(1, "a"))
        builder.apply_transaction(transaction)
        builder.apply_transaction(transaction)
        self.assertEqual(builder.received_commit_seqs, (1,))

    def test_reusing_a_commit_number_for_other_changes_is_rejected(self) -> None:
        builder = UniqueIndexBuilder(watermark=2, expected_snapshot_batches=["b1"])
        builder.apply_transaction(tx(1, Change.update(1, "a")))
        with self.assertRaises(TransactionConflictError):
            builder.apply_transaction(tx(1, Change.update(1, "b")))

    def test_transaction_at_or_below_the_snapshot_floor_is_stale(self) -> None:
        builder = UniqueIndexBuilder(
            watermark=5, expected_snapshot_batches=["b1"], snapshot_floor=3
        )
        with self.assertRaises(StaleTransactionError):
            builder.apply_transaction(tx(2, Change.update(1, "a")))
        builder.apply_transaction(tx(3, Change.update(1, "a")))
        self.assertEqual(builder.received_commit_seqs, (3,))

    def test_randomised_replay_matches_the_reference_model(self) -> None:
        import random

        rng = random.Random(20261121)
        base = {key: "v{}".format(key) for key in range(1, 21)}
        builder = UniqueIndexBuilder(
            watermark=40,
            expected_snapshot_batches=["b0", "b1", "b2", "b3"],
        )
        items = list(base.items())
        for index in range(0, len(items), 5):
            builder.add_snapshot_batch(
                "b{}".format(index // 5), items[index : index + 5]
            )

        transactions = []
        next_value = 1000
        for seq in range(1, 41):
            key = rng.randint(1, 20)
            roll = rng.random()
            if roll < 0.25:
                transactions.append(tx(seq, Change.delete(key)))
            else:
                next_value += 1
                transactions.append(tx(seq, Change.update(key, next_value)))

        # Submit out of order on purpose.
        shuffled = list(transactions)
        rng.shuffle(shuffled)
        for transaction in shuffled:
            builder.apply_transaction(transaction)

        expected = state_after(base, transactions, upto=40)
        builder.publish()
        self.assertIndexMatches(builder, expected)


if __name__ == "__main__":
    unittest.main()
