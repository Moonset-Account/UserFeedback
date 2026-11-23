"""End-to-end timeline scenarios required by the task statement.

The headline object has, in this order:

* ``v1`` - an archived version that is temporarily readable through a
  restore lease,
* ``v2`` - a live version that is still protected by retention and a hold,
* ``d1`` - the newest record, a delete marker with an expired deadline.

Every state transition is checked for both the read view and the destroyable
list, so the conservative delete-marker rule is visible step by step.
"""

from __future__ import annotations

import threading
import unittest

from version_lifecycle import (
    ExecutionReport,
    ReadState,
    ReceiptStatus,
    RetentionReason,
    VersionedStore,
)


class RestorationTimelineTests(unittest.TestCase):
    KEY = "objects/report"

    def setUp(self) -> None:
        self.store = VersionedStore()
        self.store.put_version(self.KEY, "v1", archived=True, retain_until=5)
        self.store.put_version(self.KEY, "v2", retain_until=20, holds=["legal"])
        self.store.put_delete_marker(self.KEY, "d1", retain_until=10)

    def read(self, now: float):
        return self.store.read(self.KEY, now=now)

    def destroyable(self, now: float) -> tuple[str, ...]:
        return self.store.destroyable_version_ids(self.KEY, now=now)

    def test_timeline_of_visibility_and_destroyable_sets(self) -> None:
        # --- t=0: delete marker on top, every deadline still running -------
        result = self.read(0)
        self.assertEqual(result.state, ReadState.DELETED)
        self.assertEqual(result.version_id, "d1")
        self.assertEqual(
            self.store.read(self.KEY, now=0, version_id="v1").state,
            ReadState.ARCHIVED,
        )
        self.assertEqual(self.destroyable(0), ())
        for version_id in ("v1", "v2", "d1"):
            self.assertEqual(
                self.store.removal_blocker(self.KEY, version_id, now=0),
                RetentionReason.RETENTION_NOT_REACHED,
            )

        # --- t=5: v1 retention reached, restore lease keeps it readable ----
        self.store.grant_lease(
            self.KEY, "v1", "restore-1", granted_at=5, expires_at=30
        )
        self.assertEqual(self.read(5).state, ReadState.DELETED)
        self.assertEqual(self.read(5).version_id, "d1")
        leased = self.store.read(self.KEY, now=5, version_id="v1")
        self.assertEqual(leased.state, ReadState.VISIBLE)
        self.assertEqual(leased.reason, "archived_with_active_read_lease")
        self.assertEqual(self.destroyable(5), ())
        self.assertEqual(
            self.store.removal_blocker(self.KEY, "v1", now=5),
            RetentionReason.LEASE_ACTIVE,
        )

        # --- t=10: marker deadline passes but its guard still holds --------
        self.assertEqual(
            self.store.removal_blocker(self.KEY, "d1", now=10),
            RetentionReason.DELETE_MARKER_GUARD,
        )
        self.assertEqual(self.read(10).state, ReadState.DELETED)
        self.assertEqual(self.destroyable(10), ())

        # --- t=20: v2 retention reached, independent hold still applies ----
        self.assertEqual(
            self.store.removal_blocker(self.KEY, "v2", now=20),
            RetentionReason.HELD,
        )
        self.assertEqual(self.destroyable(20), ())

        # --- t=25: hold released, only v2 is swept -------------------------
        self.store.release_hold(self.KEY, "v2", "legal")
        self.assertEqual(self.destroyable(25), ("v2",))
        self.assertEqual(self.read(25).state, ReadState.DELETED)
        # v1's restore lease is still running, so it is not yet removable.
        self.assertEqual(
            self.store.removal_blocker(self.KEY, "v1", now=25),
            RetentionReason.LEASE_ACTIVE,
        )

        plan = self.store.scan_expiry(now=25)
        self.assertEqual([i.version_id for i in plan.items], ["v2"])
        report = self.store.execute_plan(plan, now=25)
        self.assertEqual(report.removed_count, 1)

        # --- t=30: lease expired; v1 archived again and now removable ------
        self.assertEqual(
            self.store.read(self.KEY, now=30, version_id="v1").state,
            ReadState.ARCHIVED,
        )
        self.assertEqual(self.read(30).state, ReadState.DELETED)
        self.assertEqual(self.destroyable(30), ("v1",))

        plan = self.store.scan_expiry(now=30)
        self.assertEqual([i.version_id for i in plan.items], ["v1"])
        self.store.execute_plan(plan, now=30)

        # --- t=30 after v1 removed: default still DELETED, d1 now free -----
        self.assertEqual(self.read(30).state, ReadState.DELETED)
        # d1's guard held while *any* older record existed; with v1 gone the
        # marker is finally destroyable and the key disappears.
        self.assertEqual(self.destroyable(30), ("d1",))

        plan = self.store.scan_expiry(now=30)
        self.assertEqual([i.version_id for i in plan.items], ["d1"])
        self.store.execute_plan(plan, now=30)

        # --- final: key gone, default read MISSING -------------------------
        self.assertEqual(self.read(30).state, ReadState.MISSING)
        self.assertFalse(self.store.has_key(self.KEY))

    def test_restored_version_can_be_unarchived_and_read_by_default(self) -> None:
        self.store.unarchive(self.KEY, "v1")
        self.assertEqual(self.read(0).state, ReadState.DELETED)
        # The marker still shadows the default read even though v1 is live.
        self.assertEqual(
            self.store.read(self.KEY, now=0, version_id="v1").state,
            ReadState.VISIBLE,
        )

    def test_marker_deadline_passed_at_plan_time_is_still_excluded(self) -> None:
        plan = self.store.scan_expiry(now=1000)
        # v1 is past retention; v2 is past retention but still held; the
        # expired marker is never planned because it still shadows v1 and v2.
        self.assertEqual([i.version_id for i in plan.items], ["v1"])
        self.assertNotIn("d1", [i.version_id for i in plan.items])
        self.assertEqual(
            self.store.removal_blocker(self.KEY, "d1", now=1000),
            RetentionReason.DELETE_MARKER_GUARD,
        )


class PlanExecuteInterleavingTests(unittest.TestCase):
    def test_plan_item_re_adjudicated_after_lease_is_granted(self) -> None:
        store = VersionedStore()
        store.put_version("k", "archived", archived=True)
        plan = store.scan_expiry(now=100)
        self.assertEqual([i.version_id for i in plan.items], ["archived"])

        store.grant_lease(
            "k", "archived", "restore", granted_at=90, expires_at=200
        )
        report = store.execute_plan(plan, now=100)
        self.assertEqual(report.receipts[0].status, ReceiptStatus.RETAINED)
        self.assertEqual(report.receipts[0].reason, RetentionReason.LEASE_ACTIVE.value)
        self.assertEqual(store.read("k", now=100).state, ReadState.VISIBLE)

    def test_plan_item_re_adjudicated_after_delete_marker_appears(self) -> None:
        store = VersionedStore()
        store.put_version("k", "v1")
        store.put_version("k", "v2")
        plan = store.scan_expiry(now=0)
        store.put_delete_marker("k", "d1")
        # Both planned items still exist and are still data versions, so both
        # are removed; the new marker is simply not part of this plan.
        report = store.execute_plan(plan, now=0)
        self.assertEqual(report.removed_count, 2)
        self.assertEqual(store.read("k", now=0).state, ReadState.DELETED)
        self.assertEqual([v.version_id for v in store.versions("k")], ["d1"])

    def test_execute_batches_in_any_order_yields_same_final_state(self) -> None:
        store = VersionedStore()
        for key in ("a", "b", "c"):
            store.put_version(key, f"{key}1")
            store.put_version(key, f"{key}2")
        plan = store.scan_expiry(now=0, batch_size=2)
        self.assertEqual(plan.batch_count, 3)
        for batch in reversed(plan.batches):
            store.execute_plan(plan, now=0, batch_ids=[batch.batch_id])
        self.assertEqual(store.keys(), ())
        replay = store.replay(plan)
        self.assertEqual(len(replay.receipts), 6)
        self.assertTrue(all(r.status is ReceiptStatus.REMOVED for r in replay.receipts))


class ConcurrentExecutionTests(unittest.TestCase):
    def test_concurrent_batch_execution_removes_each_item_exactly_once(self) -> None:
        store = VersionedStore()
        for index in range(8):
            store.put_version("k", f"v{index}")
        plan = store.scan_expiry(now=0, batch_size=1)
        self.assertEqual(plan.batch_count, 8)

        reports: list[ExecutionReport] = []
        errors: list[BaseException] = []
        barrier = threading.Barrier(len(plan.batches))

        def worker(batch_id: str) -> None:
            try:
                barrier.wait(timeout=5)
                reports.append(
                    store.execute_plan(plan, now=0, batch_ids=[batch_id])
                )
            except BaseException as exc:  # pragma: no cover - failure path
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(batch.batch_id,))
            for batch in plan.batches
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(errors, [])
        self.assertEqual(len(reports), 8)
        all_receipts = [r for report in reports for r in report.receipts]
        self.assertEqual(len(all_receipts), 8)
        self.assertEqual(
            {r.item_id for r in all_receipts},
            {item.item_id for item in plan.items},
        )
        self.assertEqual(sum(1 for r in all_receipts if r.removed), 8)
        self.assertFalse(store.has_key("k"))
        self.assertEqual(len(store.receipt_log()), 8)


if __name__ == "__main__":
    unittest.main()
