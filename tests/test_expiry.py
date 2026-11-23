"""Expiry scanning, batched plans, re-adjudication and receipt replay."""

from __future__ import annotations

import unittest

from version_lifecycle import (
    ExecutionReceipt,
    ReadState,
    ReceiptStatus,
    RetentionReason,
    VersionedStore,
)


def statuses(report) -> dict[str, ReceiptStatus]:
    return {(r.key, r.version_id): r.status for r in report.receipts}


class ScanPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = VersionedStore()

    def test_plan_lists_only_destroyable_versions(self) -> None:
        self.store.put_version("a", "a1", retain_until=100)
        self.store.put_version("a", "a2")
        self.store.put_version("b", "b1", holds=["legal"])
        plan = self.store.scan_expiry(now=0, batch_size=10)
        self.assertEqual(
            [(item.key, item.version_id) for item in plan.items], [("a", "a2")]
        )
        self.assertEqual(plan.item_count, 1)
        self.assertEqual(plan.batch_count, 1)

    def test_plan_is_batched_in_key_then_sequence_order(self) -> None:
        for key in ("b", "a", "c"):
            self.store.put_version(key, f"{key}1")
            self.store.put_version(key, f"{key}2")
        plan = self.store.scan_expiry(now=0, batch_size=4)
        self.assertEqual(plan.item_count, 6)
        self.assertEqual(plan.batch_count, 2)
        self.assertEqual(
            [(i.key, i.version_id) for i in plan.batches[0].items],
            [("a", "a1"), ("a", "a2"), ("b", "b1"), ("b", "b2")],
        )
        self.assertEqual(
            [(i.key, i.version_id) for i in plan.batches[1].items],
            [("c", "c1"), ("c", "c2")],
        )

    def test_plan_item_ids_are_stable_and_unique(self) -> None:
        self.store.put_version("a", "a1")
        self.store.put_version("a", "a2")
        plan = self.store.scan_expiry(now=0, batch_size=1)
        ids = [item.item_id for item in plan.items]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(i.startswith(plan.plan_id) for i in ids))
        self.assertEqual(
            ids, [f"{plan.plan_id}#item-0000", f"{plan.plan_id}#item-0001"]
        )

    def test_plan_records_snapshot_metadata(self) -> None:
        self.store.put_version("a", "a1", retain_until=5)
        self.store.put_version("a", "a2", archived=True)
        self.store.grant_lease("a", "a2", "restore", granted_at=50, expires_at=100)
        plan = self.store.scan_expiry(now=200)
        items = {item.version_id: item for item in plan.items}
        self.assertEqual(items["a1"].retain_until, 5.0)
        self.assertEqual(items["a1"].hold_ids, ())
        self.assertIsNone(items["a1"].lease_id)
        self.assertEqual(items["a2"].lease_id, "restore")
        self.assertEqual(items["a2"].planned_at, 200.0)
        for item in items.values():
            self.assertEqual(item.action, "remove")
            self.assertEqual(item.reason, "eligible_for_removal")

    def test_plan_ids_increase_and_plans_are_registered(self) -> None:
        self.store.put_version("a", "a1")
        first = self.store.scan_expiry(now=0)
        second = self.store.scan_expiry(now=0)
        self.assertNotEqual(first.plan_id, second.plan_id)
        self.assertIs(self.store.plan(first.plan_id), first)

    def test_unknown_key_filter_rejected(self) -> None:
        self.store.put_version("a", "a1")
        with self.assertRaises(Exception):
            self.store.scan_expiry(now=0, keys=["a", "missing"])

    def test_duplicate_key_filter_is_deduplicated(self) -> None:
        self.store.put_version("a", "a1")
        plan = self.store.scan_expiry(now=0, keys=["a", "a"])
        self.assertEqual([i.version_id for i in plan.items], ["a1"])

    def test_invalid_batch_size_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.store.scan_expiry(now=0, batch_size=0)
        with self.assertRaises(ValueError):
            self.store.scan_expiry(now=0, batch_size=-1)
        with self.assertRaises(TypeError):
            self.store.scan_expiry(now=0, batch_size=2.5)
        with self.assertRaises(TypeError):
            self.store.scan_expiry(now=0, batch_size=True)

    def test_empty_plan(self) -> None:
        self.store.put_version("a", "a1", retain_until=100)
        plan = self.store.scan_expiry(now=0)
        self.assertEqual(plan.item_count, 0)
        self.assertEqual(plan.batches, ())
        report = self.store.execute_plan(plan, now=0)
        self.assertEqual(report.receipts, ())

    def test_scan_can_be_limited_to_keys(self) -> None:
        self.store.put_version("a", "a1")
        self.store.put_version("b", "b1")
        plan = self.store.scan_expiry(now=0, keys=["b"])
        self.assertEqual([i.key for i in plan.items], ["b"])


class ExecutePlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = VersionedStore()

    def test_plain_execution_removes_everything(self) -> None:
        self.store.put_version("a", "a1", retain_until=10)
        self.store.put_version("a", "a2")
        plan = self.store.scan_expiry(now=10, batch_size=1)
        report = self.store.execute_plan(plan, now=10)
        self.assertEqual(report.removed_count, 2)
        self.assertEqual(report.retained_count, 0)
        self.assertFalse(self.store.has_key("a"))
        for receipt in report.receipts:
            self.assertTrue(receipt.removed)
            self.assertIs(receipt.status, ReceiptStatus.REMOVED)
            self.assertEqual(receipt.executed_at, 10.0)

    def test_new_hold_after_planning_retains_item(self) -> None:
        self.store.put_version("a", "a1")
        self.store.put_version("a", "a2")
        plan = self.store.scan_expiry(now=0)
        self.store.add_hold("a", "a2", "late-hold")
        report = self.store.execute_plan(plan, now=0)
        self.assertEqual(
            statuses(report),
            {("a", "a1"): ReceiptStatus.REMOVED, ("a", "a2"): ReceiptStatus.RETAINED},
        )
        retained = next(r for r in report.receipts if r.removed is False)
        self.assertEqual(retained.reason, RetentionReason.HELD.value)
        self.assertEqual([v.version_id for v in self.store.versions("a")], ["a2"])

    def test_retention_extension_after_planning_retains_item(self) -> None:
        self.store.put_version("a", "a1", retain_until=10)
        plan = self.store.scan_expiry(now=10)
        self.store.set_retention("a", "a1", 50)
        report = self.store.execute_plan(plan, now=10)
        self.assertEqual(report.receipts[0].status, ReceiptStatus.RETAINED)
        self.assertEqual(
            report.receipts[0].reason, RetentionReason.RETENTION_NOT_REACHED.value
        )

    def test_lease_granted_after_planning_retains_item(self) -> None:
        self.store.put_version("a", "a1", archived=True)
        plan = self.store.scan_expiry(now=100)
        self.store.grant_lease(
            "a", "a1", "restore", granted_at=100, expires_at=200
        )
        report = self.store.execute_plan(plan, now=100)
        self.assertEqual(report.receipts[0].status, ReceiptStatus.RETAINED)
        self.assertEqual(report.receipts[0].reason, RetentionReason.LEASE_ACTIVE.value)

    def test_item_becomes_eligible_before_execution(self) -> None:
        self.store.put_version("a", "a1")
        plan = self.store.scan_expiry(now=0)
        # A second scan at a later instant would include nothing new here, but
        # executing the old plan later must still use live state: the version
        # is still eligible, so it is removed at the later instant.
        report = self.store.execute_plan(plan, now=500)
        self.assertEqual(report.receipts[0].status, ReceiptStatus.REMOVED)
        self.assertEqual(report.receipts[0].executed_at, 500.0)

    def test_version_removed_before_execution_is_missing(self) -> None:
        self.store.put_version("a", "a1")
        self.store.put_version("a", "a2")
        plan = self.store.scan_expiry(now=0)
        self.store.remove_version("a", "a1", now=0)
        report = self.store.execute_plan(plan, now=0)
        self.assertEqual(
            statuses(report),
            {
                ("a", "a1"): ReceiptStatus.MISSING_VERSION,
                ("a", "a2"): ReceiptStatus.REMOVED,
            },
        )
        a1 = next(r for r in report.receipts if r.version_id == "a1")
        self.assertIsNone(a1.observed_sequence)
        self.assertEqual(a1.planned_sequence, 0)
        self.assertFalse(a1.removed)

    def test_whole_key_removed_before_execution(self) -> None:
        self.store.put_version("a", "a1")
        plan = self.store.scan_expiry(now=0)
        self.store.remove_version("a", "a1", now=0)
        report = self.store.execute_plan(plan, now=0)
        self.assertEqual(report.receipts[0].status, ReceiptStatus.MISSING_KEY)

    def test_recreated_version_id_is_stale_not_deleted(self) -> None:
        self.store.put_version("a", "a1")
        plan = self.store.scan_expiry(now=0)
        self.store.remove_version("a", "a1", now=0)
        recreated = self.store.put_version("a", "a1", retain_until=10_000)
        self.assertEqual(recreated.sequence, 1)
        report = self.store.execute_plan(plan, now=0)
        receipt = report.receipts[0]
        self.assertEqual(receipt.status, ReceiptStatus.STALE_VERSION)
        self.assertEqual(receipt.planned_sequence, 0)
        self.assertEqual(receipt.observed_sequence, 1)
        # The recreated record is untouched.
        self.assertEqual(self.store.get_version("a", "a1").sequence, 1)

    def test_execution_is_idempotent_by_item_id(self) -> None:
        self.store.put_version("a", "a1")
        self.store.put_version("a", "a2")
        plan = self.store.scan_expiry(now=0)
        first = self.store.execute_plan(plan, now=0)
        second = self.store.execute_plan(plan, now=5)
        self.assertEqual(
            [r.item_id for r in first.receipts], [r.item_id for r in second.receipts]
        )
        self.assertEqual(
            [r.status for r in second.receipts],
            [ReceiptStatus.REMOVED, ReceiptStatus.REMOVED],
        )
        # Same receipt objects replayed, not fresh adjudications.
        self.assertEqual(first.receipts, second.receipts)
        self.assertEqual(
            second.replayed_item_ids, tuple(r.item_id for r in second.receipts)
        )
        self.assertEqual(second.removed_now_count, 0)
        self.assertFalse(self.store.has_key("a"))

    def test_retained_receipt_is_frozen_for_later_retries(self) -> None:
        """A receipt is the record of one adjudication, not a live verdict.

        ``a1`` is held when the plan runs, so its receipt says ``RETAINED``.
        After the hold is released the same item ID still replays the
        original ``RETAINED`` receipt; a fresh scan is what picks the
        version up again.
        """

        self.store.put_version("a", "a1")
        plan = self.store.scan_expiry(now=0)
        self.store.add_hold("a", "a1", "hold-1")
        first = self.store.execute_plan(plan, now=0)
        self.assertEqual(first.receipts[0].status, ReceiptStatus.RETAINED)

        self.store.release_hold("a", "a1", "hold-1")
        retried = self.store.execute_plan(plan, now=100)
        self.assertEqual(retried.receipts[0], first.receipts[0])
        self.assertEqual(retried.receipts[0].executed_at, 0.0)
        self.assertEqual(retried.removed_now_count, 0)
        self.assertEqual([v.version_id for v in self.store.versions("a")], ["a1"])

        # A new scan at the later instant finds the version eligible.
        fresh = self.store.scan_expiry(now=100)
        self.assertEqual([i.version_id for i in fresh.items], ["a1"])
        report = self.store.execute_plan(fresh, now=100)
        self.assertEqual(report.receipts[0].status, ReceiptStatus.REMOVED)
        self.assertFalse(self.store.has_key("a"))

    def test_batch_selection_and_retry_of_one_batch(self) -> None:
        for index in range(4):
            self.store.put_version("a", f"a{index}")
        plan = self.store.scan_expiry(now=0, batch_size=2)
        self.assertEqual(plan.batch_count, 2)

        first = self.store.execute_plan(
            plan, now=0, batch_ids=[plan.batches[0].batch_id]
        )
        self.assertEqual(len(first.receipts), 2)
        self.assertEqual(first.batch_id, plan.batches[0].batch_id)
        self.assertEqual([v.version_id for v in self.store.versions("a")], ["a2", "a3"])

        retry = self.store.execute_plan(
            plan, now=10, batch_ids=[plan.batches[0].batch_id]
        )
        self.assertEqual(retry.receipts, first.receipts)
        self.assertEqual(retry.removed_now_count, 0)

        second = self.store.execute_plan(
            plan, now=10, batch_ids=[plan.batches[1].batch_id]
        )
        self.assertEqual(second.removed_count, 2)
        self.assertFalse(self.store.has_key("a"))

    def test_unknown_batch_rejected(self) -> None:
        with self.assertRaises(Exception):
            self.store.execute_plan("plan-999999", now=0)
        self.store.put_version("a", "a1")
        plan = self.store.scan_expiry(now=0)
        with self.assertRaises(Exception):
            self.store.execute_plan(plan, now=0, batch_ids=["nope"])

    def test_replay_does_not_mutate(self) -> None:
        self.store.put_version("a", "a1")
        self.store.put_version("a", "a2")
        plan = self.store.scan_expiry(now=0)
        empty = self.store.replay(plan)
        self.assertEqual(empty.receipts, ())
        report = self.store.execute_plan(plan, now=0)
        replayed = self.store.replay(plan)
        self.assertEqual(replayed.receipts, report.receipts)
        self.assertEqual(
            replayed.replayed_item_ids, tuple(r.item_id for r in report.receipts)
        )

    def test_receipt_lookup_by_item_id(self) -> None:
        self.store.put_version("a", "a1")
        plan = self.store.scan_expiry(now=0)
        item_id = plan.items[0].item_id
        self.assertIsNone(self.store.receipt(item_id))
        report = self.store.execute_plan(plan, now=0)
        receipt = self.store.get_receipt(item_id)
        self.assertIsInstance(receipt, ExecutionReceipt)
        self.assertEqual(receipt, report.receipts[0])
        self.assertEqual(self.store.receipt_log(), (receipt,))
        self.assertEqual(self.store.replay_item(item_id), receipt)
        with self.assertRaises(Exception):
            self.store.replay_item("no-such-item")


class InterleavedBatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = VersionedStore()

    def test_stacked_markers_are_swept_without_resurrection(self) -> None:
        """Plan/execute state transitions, checked step by step.

        All three records are destroyable: clearing either marker still
        leaves ``DELETED`` (by the other marker) as the default read.  One
        sweep removes ``v1``, ``d1`` and ``d2`` in plan order, each
        re-adjudicated live, and the default read goes ``DELETED`` ->
        ``MISSING`` only after the last record is gone.
        """

        self.store.put_version("k", "v1")
        self.store.put_delete_marker("k", "d1")
        self.store.put_delete_marker("k", "d2")

        self.assertEqual(self.store.read("k", now=0).state, ReadState.DELETED)
        self.assertEqual(
            self.store.destroyable_version_ids("k", now=0), ("v1", "d1", "d2")
        )

        plan = self.store.scan_expiry(now=0, batch_size=2)
        self.assertEqual([i.version_id for i in plan.items], ["v1", "d1", "d2"])

        first = self.store.execute_plan(
            plan, now=0, batch_ids=[plan.batches[0].batch_id]
        )
        self.assertEqual(
            [r.status for r in first.receipts],
            [ReceiptStatus.REMOVED, ReceiptStatus.REMOVED],
        )
        # Transition: only d2 left, still DELETED, still destroyable.
        self.assertEqual(self.store.read("k", now=0).state, ReadState.DELETED)
        self.assertEqual(self.store.destroyable_version_ids("k", now=0), ("d2",))

        second = self.store.execute_plan(
            plan, now=0, batch_ids=[plan.batches[1].batch_id]
        )
        self.assertEqual(second.receipts[0].status, ReceiptStatus.REMOVED)
        self.assertEqual(self.store.read("k", now=0).state, ReadState.MISSING)
        self.assertFalse(self.store.has_key("k"))

    def test_surviving_marker_is_retained_once_it_shadows_data(self) -> None:
        """With ``v1`` held, the marker above it must survive the sweep.

        Both markers look destroyable while two of them exist.  Execution
        runs ``d1`` first (still shadowed by ``d2``, so removed), and ``d2``
        is then re-adjudicated as the only marker over the held ``v1`` and
        retained.  The default read never leaves ``DELETED``.
        """

        self.store.put_version("k", "v1", holds=["litigation"])
        self.store.put_delete_marker("k", "d1")
        self.store.put_delete_marker("k", "d2")

        self.assertEqual(
            self.store.destroyable_version_ids("k", now=0), ("d1", "d2")
        )
        plan = self.store.scan_expiry(now=0)
        self.assertEqual([i.version_id for i in plan.items], ["d1", "d2"])

        report = self.store.execute_plan(plan, now=0)
        self.assertEqual(
            statuses(report),
            {("k", "d1"): ReceiptStatus.REMOVED, ("k", "d2"): ReceiptStatus.RETAINED},
        )
        self.assertEqual(
            report.receipts[1].reason, RetentionReason.DELETE_MARKER_GUARD.value
        )
        self.assertEqual(self.store.read("k", now=0).state, ReadState.DELETED)
        self.assertEqual(self.store.destroyable_version_ids("k", now=0), ())

        # Releasing the hold leaves the marker pinned: the data version is
        # now sweepable, but only while d2 still shadows it.
        self.store.release_hold("k", "v1", "litigation")
        self.assertEqual(self.store.destroyable_version_ids("k", now=0), ("v1",))
        third = self.store.scan_expiry(now=0)
        self.assertEqual([i.version_id for i in third.items], ["v1"])
        self.store.execute_plan(third, now=0)
        self.assertEqual(self.store.read("k", now=0).state, ReadState.DELETED)
        self.assertEqual(self.store.destroyable_version_ids("k", now=0), ("d2",))

    def test_marker_and_shadowed_version_need_two_sweeps(self) -> None:
        self.store.put_version("k", "v1")
        self.store.put_delete_marker("k", "d1", retain_until=10)

        # At expiry the marker is retained: removing it would resurrect v1.
        self.assertEqual(
            self.store.removal_blocker("k", "d1", now=10), RetentionReason.DELETE_MARKER_GUARD
        )
        first = self.store.scan_expiry(now=10)
        self.assertEqual([i.version_id for i in first.items], ["v1"])
        self.store.execute_plan(first, now=10)

        self.assertEqual(self.store.read("k", now=10).state, ReadState.DELETED)
        self.assertEqual(self.store.destroyable_version_ids("k", now=10), ("d1",))

        second = self.store.scan_expiry(now=10)
        self.assertEqual([i.version_id for i in second.items], ["d1"])
        self.store.execute_plan(second, now=10)
        self.assertFalse(self.store.has_key("k"))

    def test_mixed_interleaving_with_hold_added_mid_batch(self) -> None:
        self.store.put_version("a", "a1")
        self.store.put_version("b", "b1")
        self.store.put_version("b", "b2")
        plan = self.store.scan_expiry(now=0, batch_size=10)
        self.assertEqual(plan.item_count, 3)
        # Mutate after planning: hold b2, replace a1 with a fresh sequence.
        self.store.add_hold("b", "b2", "audit")
        self.store.remove_version("a", "a1", now=0)
        self.store.put_version("a", "a1")
        report = self.store.execute_plan(plan, now=0)
        self.assertEqual(
            statuses(report),
            {
                ("a", "a1"): ReceiptStatus.STALE_VERSION,
                ("b", "b1"): ReceiptStatus.REMOVED,
                ("b", "b2"): ReceiptStatus.RETAINED,
            },
        )
        self.assertEqual([v.version_id for v in self.store.versions("a")], ["a1"])
        self.assertEqual([v.version_id for v in self.store.versions("b")], ["b2"])


if __name__ == "__main__":
    unittest.main()
