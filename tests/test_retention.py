"""Retention, holds, read leases and the delete-marker guard."""

from __future__ import annotations

import unittest

from version_lifecycle import (
    LifecycleError,
    ReadState,
    RetentionBlockedError,
    RetentionReason,
    VersionedStore,
)


class RetentionBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = VersionedStore()

    def test_retention_is_reached_at_the_exact_deadline(self) -> None:
        self.store.put_version("k", "v1", retain_until=100)
        self.assertEqual(
            self.store.removal_blocker("k", "v1", now=99.999),
            RetentionReason.RETENTION_NOT_REACHED,
        )
        self.assertIsNone(self.store.removal_blocker("k", "v1", now=100))
        self.assertTrue(self.store.is_destroyable("k", "v1", now=100))

    def test_no_retention_means_immediately_destroyable(self) -> None:
        self.store.put_version("k", "v1")
        self.assertTrue(self.store.is_destroyable("k", "v1", now=0))

    def test_hold_blocks_until_released(self) -> None:
        self.store.put_version("k", "v1", retain_until=10)
        self.store.add_hold("k", "v1", "legal")
        self.assertEqual(
            self.store.removal_blocker("k", "v1", now=50), RetentionReason.HELD
        )
        self.store.release_hold("k", "v1", "legal")
        self.assertIsNone(self.store.removal_blocker("k", "v1", now=50))

    def test_multiple_holds_all_must_be_released(self) -> None:
        self.store.put_version("k", "v1", holds=["a", "b"])
        self.store.release_hold("k", "v1", "a")
        self.assertEqual(
            self.store.removal_blocker("k", "v1", now=0), RetentionReason.HELD
        )
        self.store.release_hold("k", "v1", "b")
        self.assertIsNone(self.store.removal_blocker("k", "v1", now=0))

    def test_releasing_absent_hold_rejected(self) -> None:
        self.store.put_version("k", "v1")
        with self.assertRaises(LifecycleError):
            self.store.release_hold("k", "v1", "missing")

    def test_active_read_lease_blocks_removal(self) -> None:
        self.store.put_version("k", "v1", archived=True)
        self.store.grant_lease("k", "v1", "restore", granted_at=0, expires_at=10)
        self.assertEqual(
            self.store.removal_blocker("k", "v1", now=5),
            RetentionReason.LEASE_ACTIVE,
        )
        self.assertIsNone(self.store.removal_blocker("k", "v1", now=10))

    def test_blocker_priority_retention_then_hold_then_lease(self) -> None:
        self.store.put_version("k", "v1", retain_until=10, holds=["h"], archived=True)
        self.store.grant_lease("k", "v1", "restore", granted_at=0, expires_at=30)
        self.assertEqual(
            self.store.removal_blocker("k", "v1", now=5),
            RetentionReason.RETENTION_NOT_REACHED,
        )
        self.assertEqual(
            self.store.removal_blocker("k", "v1", now=20), RetentionReason.HELD
        )
        self.store.release_hold("k", "v1", "h")
        self.assertEqual(
            self.store.removal_blocker("k", "v1", now=20),
            RetentionReason.LEASE_ACTIVE,
        )
        self.assertIsNone(self.store.removal_blocker("k", "v1", now=30))

    def test_set_retention_extends_and_clears(self) -> None:
        self.store.put_version("k", "v1", retain_until=10)
        self.store.set_retention("k", "v1", 100)
        self.assertEqual(
            self.store.removal_blocker("k", "v1", now=50),
            RetentionReason.RETENTION_NOT_REACHED,
        )
        self.store.set_retention("k", "v1", None)
        self.assertIsNone(self.store.removal_blocker("k", "v1", now=50))

    def test_delete_marker_retention_applies_to_marker(self) -> None:
        self.store.put_version("k", "v1")
        self.store.put_delete_marker("k", "d1", retain_until=100)
        self.assertEqual(
            self.store.removal_blocker("k", "d1", now=99),
            RetentionReason.RETENTION_NOT_REACHED,
        )
        self.assertEqual(
            self.store.removal_blocker("k", "d1", now=100),
            RetentionReason.DELETE_MARKER_GUARD,
        )


class DeleteMarkerGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = VersionedStore()

    def test_expired_marker_stays_while_it_shadows_a_data_version(self) -> None:
        self.store.put_version("k", "v1")
        self.store.put_delete_marker("k", "d1", retain_until=10)
        self.assertEqual(
            self.store.removal_blocker("k", "d1", now=100),
            RetentionReason.DELETE_MARKER_GUARD,
        )
        self.assertFalse(self.store.is_destroyable("k", "d1", now=100))

    def test_expired_marker_stays_even_if_older_version_is_archived(self) -> None:
        self.store.put_version("k", "v1", archived=True)
        self.store.put_delete_marker("k", "d1")
        # Clearing d1 would flip the default read from DELETED to ARCHIVED, so
        # the guard still applies.
        self.assertEqual(
            self.store.removal_blocker("k", "d1", now=0),
            RetentionReason.DELETE_MARKER_GUARD,
        )
    def test_marker_destroyable_once_nothing_older_remains(self) -> None:
        self.store.put_version("k", "v1")
        self.store.put_delete_marker("k", "d1")
        self.assertTrue(self.store.is_destroyable("k", "v1", now=0))
        self.store.remove_version("k", "v1", now=0)
        self.assertIsNone(self.store.removal_blocker("k", "d1", now=0))

    def test_only_a_marker_shadowing_data_is_guarded(self) -> None:
        self.store.put_version("k", "v1")
        self.store.put_delete_marker("k", "d1")
        self.store.put_delete_marker("k", "d2")
        # Clearing d1 or d2 cannot make a data version the default read: the
        # other marker still answers DELETED.
        self.assertTrue(self.store.is_destroyable("k", "d1", now=0))
        self.assertTrue(self.store.is_destroyable("k", "d2", now=0))
        self.assertEqual(self.store.read("k", now=0).state, ReadState.DELETED)

        # Removing d1 is safe; d2 is still newest and now directly shadows
        # v1, so d2 becomes guarded.
        self.store.remove_version("k", "d1", now=0)
        self.assertEqual(
            self.store.removal_blocker("k", "d2", now=0),
            RetentionReason.DELETE_MARKER_GUARD,
        )
        self.assertEqual(self.store.read("k", now=0).state, ReadState.DELETED)

    def test_marker_with_newer_data_version_is_destroyable(self) -> None:
        self.store.put_version("k", "v1")
        self.store.put_delete_marker("k", "d1")
        self.store.put_version("k", "v2")
        self.assertIsNone(self.store.removal_blocker("k", "d1", now=0))
        self.assertEqual(self.store.read("k", now=0).version_id, "v2")

    def test_newest_marker_without_older_records_is_destroyable(self) -> None:
        self.store.put_delete_marker("k", "d1")
        self.assertIsNone(self.store.removal_blocker("k", "d1", now=0))
        self.store.remove_version("k", "d1", now=0)
        self.assertEqual(self.store.read("k", now=0).state, ReadState.MISSING)


class DestroyableListTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = VersionedStore()

    def test_destroyable_report_is_grouped_and_sorted(self) -> None:
        self.store.put_version("b", "b1")
        self.store.put_version("a", "a1", retain_until=10)
        self.store.put_version("a", "a2")
        report = self.store.destroyable(now=0)
        self.assertEqual(report.as_of, 0.0)
        self.assertEqual(
            report.by_key, {"a": ("a2",), "b": ("b1",)}
        )
        self.assertEqual(report.version_ids, ("a2", "b1"))
        self.assertEqual(report.total, 2)

    def test_destroyable_list_at_later_instant_includes_expired(self) -> None:
        self.store.put_version("a", "a1", retain_until=10)
        self.store.put_version("b", "b1", holds=["h"])
        self.assertEqual(self.store.destroyable(now=0).by_key, {})
        late = self.store.destroyable(now=10)
        self.assertEqual(late.by_key, {"a": ("a1",)})
        self.store.release_hold("b", "b1", "h")
        self.assertEqual(
            self.store.destroyable(now=10).by_key, {"a": ("a1",), "b": ("b1",)}
        )

    def test_direct_removal_reports_reason(self) -> None:
        self.store.put_version("a", "a1", retain_until=10)
        with self.assertRaises(RetentionBlockedError) as ctx:
            self.store.remove_version("a", "a1", now=5)
        self.assertEqual(ctx.exception.reason, RetentionReason.RETENTION_NOT_REACHED)
        removed = self.store.remove_version("a", "a1", now=10)
        self.assertEqual(removed.version_id, "a1")
        self.assertFalse(self.store.has_key("a"))

    def test_snapshot_reports_read_and_destroyable(self) -> None:
        self.store.put_version("k", "v1", retain_until=10)
        self.store.put_version("k", "v2")
        snap = self.store.snapshot("k", now=0)
        self.assertEqual(snap.read.version_id, "v2")
        self.assertEqual(snap.destroyable_version_ids, ("v2",))
        self.assertEqual([r.version_id for r in snap.versions], ["v1", "v2"])
        snap_late = self.store.snapshot("k", now=10)
        self.assertEqual(snap_late.destroyable_version_ids, ("v1", "v2"))


if __name__ == "__main__":
    unittest.main()
