"""Visibility rules: default reads, delete-marker shadowing, archive leases."""

from __future__ import annotations

import unittest

from version_lifecycle import (
    ArchiveLease,
    LifecycleError,
    ReadState,
    VersionedStore,
)


class DefaultReadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = VersionedStore()

    def test_empty_key_reads_missing(self) -> None:
        result = self.store.read("absent", now=0)
        self.assertEqual(result.state, ReadState.MISSING)
        self.assertIsNone(result.version_id)
        self.assertEqual(result.reason, "no_versions")

    def test_newest_version_wins(self) -> None:
        self.store.put_version("k", "v1")
        self.store.put_version("k", "v2")
        self.assertEqual(self.store.read("k", now=0).version_id, "v2")
        self.assertEqual(self.store.read("k", now=0).state, ReadState.VISIBLE)

    def test_delete_marker_shadows_older_versions(self) -> None:
        self.store.put_version("k", "v1")
        self.store.put_version("k", "v2")
        self.store.put_delete_marker("k", "d1")

        default = self.store.read("k", now=0)
        self.assertEqual(default.state, ReadState.DELETED)
        self.assertEqual(default.version_id, "d1")
        self.assertEqual(default.reason, "newest_record_is_delete_marker")

        # A delete marker only affects the default read; version reads still
        # resolve older records.
        self.assertEqual(
            self.store.read("k", now=0, version_id="v1").state, ReadState.VISIBLE
        )
        self.assertEqual(
            self.store.read("k", now=0, version_id="v1").version_id, "v1"
        )

    def test_specific_marker_read_is_deleted(self) -> None:
        self.store.put_version("k", "v1")
        self.store.put_delete_marker("k", "d1")
        result = self.store.read("k", now=0, version_id="d1")
        self.assertEqual(result.state, ReadState.DELETED)
        self.assertEqual(result.reason, "version_is_delete_marker")

    def test_unknown_version_read_is_missing(self) -> None:
        self.store.put_version("k", "v1")
        result = self.store.read("k", now=0, version_id="nope")
        self.assertEqual(result.state, ReadState.MISSING)
        self.assertEqual(result.reason, "unknown_version")

    def test_new_put_after_delete_marker_becomes_visible(self) -> None:
        self.store.put_version("k", "v1")
        self.store.put_delete_marker("k", "d1")
        self.store.put_version("k", "v2")
        result = self.store.read("k", now=0)
        self.assertEqual(result.state, ReadState.VISIBLE)
        self.assertEqual(result.version_id, "v2")

    def test_sequences_are_monotonic_per_key(self) -> None:
        first = self.store.put_version("k", "v1")
        second = self.store.put_version("k", "v2")
        other = self.store.put_version("other", "v1")
        self.assertEqual((first.sequence, second.sequence), (0, 1))
        self.assertEqual(other.sequence, 0)

    def test_duplicate_version_id_rejected(self) -> None:
        self.store.put_version("k", "v1")
        with self.assertRaises(LifecycleError):
            self.store.put_version("k", "v1")


class ArchiveReadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = VersionedStore()
        self.store.put_version("k", "v1", archived=True)

    def test_archived_without_lease_is_not_readable(self) -> None:
        result = self.store.read("k", now=0)
        self.assertEqual(result.state, ReadState.ARCHIVED)
        self.assertEqual(result.reason, "archived_without_active_read_lease")

    def test_lease_makes_archived_version_temporarily_readable(self) -> None:
        self.store.grant_lease("k", "v1", "lease-1", granted_at=10, expires_at=20)
        self.assertEqual(self.store.read("k", now=9).state, ReadState.ARCHIVED)
        self.assertEqual(self.store.read("k", now=10).state, ReadState.VISIBLE)
        self.assertEqual(self.store.read("k", now=19.999).state, ReadState.VISIBLE)
        # Half-open interval: the expiry instant is already inactive.
        self.assertEqual(self.store.read("k", now=20).state, ReadState.ARCHIVED)
        self.assertEqual(self.store.read("k", now=100).state, ReadState.ARCHIVED)

    def test_lease_on_live_version_rejected(self) -> None:
        self.store.put_version("live", "v1")
        with self.assertRaises(LifecycleError):
            self.store.grant_lease(
                "live", "v1", "lease-1", granted_at=0, expires_at=1
            )

    def test_lease_on_delete_marker_rejected(self) -> None:
        self.store.put_delete_marker("k2", "d1")
        with self.assertRaises(LifecycleError):
            self.store.grant_lease(
                "k2", "d1", "lease-1", granted_at=0, expires_at=1
            )

    def test_invalid_lease_interval_rejected(self) -> None:
        with self.assertRaises(LifecycleError):
            self.store.grant_lease(
                "k", "v1", "lease-1", granted_at=5, expires_at=5
            )
        with self.assertRaises(LifecycleError):
            self.store.grant_lease(
                "k", "v1", "lease-1", granted_at=6, expires_at=5
            )

    def test_active_lease_conflict_rejected(self) -> None:
        self.store.grant_lease("k", "v1", "lease-1", granted_at=0, expires_at=10)
        with self.assertRaises(LifecycleError):
            self.store.grant_lease("k", "v1", "lease-2", granted_at=5, expires_at=15)
        # Once the first lease expired, a new one may be granted.
        record = self.store.grant_lease(
            "k", "v1", "lease-3", granted_at=10, expires_at=15
        )
        self.assertEqual(record.lease.lease_id, "lease-3")

    def test_unarchive_drops_lease_and_makes_visible(self) -> None:
        self.store.grant_lease("k", "v1", "lease-1", granted_at=0, expires_at=10)
        record = self.store.unarchive("k", "v1")
        self.assertFalse(record.archived)
        self.assertIsNone(record.lease)
        self.assertEqual(self.store.read("k", now=5).state, ReadState.VISIBLE)

    def test_archived_newest_does_not_fall_back(self) -> None:
        self.store.put_version("k2", "v1")
        self.store.put_version("k2", "v2", archived=True)
        self.assertEqual(self.store.read("k2", now=0).version_id, "v2")
        self.assertEqual(self.store.read("k2", now=0).state, ReadState.ARCHIVED)

    def test_lease_object_is_exposed(self) -> None:
        record = self.store.grant_lease(
            "k", "v1", "lease-1", granted_at=0, expires_at=10
        )
        self.assertEqual(
            record.lease, ArchiveLease(lease_id="lease-1", granted_at=0, expires_at=10)
        )
        self.assertTrue(record.lease.is_active(9))
        self.assertFalse(record.lease.is_active(10))


if __name__ == "__main__":
    unittest.main()
