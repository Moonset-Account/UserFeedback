"""Public API surface: validation, error types and read-only views."""

from __future__ import annotations

import unittest

from version_lifecycle import (
    LifecycleError,
    RetentionBlockedError,
    RetentionReason,
    UnknownKeyError,
    UnknownPlanError,
    UnknownReceiptError,
    UnknownVersionError,
    VersionedStore,
)


class ArgumentValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = VersionedStore()
        self.store.put_version("k", "v1")
        self.store.put_delete_marker("k", "d1")

    def test_non_finite_time_rejected(self) -> None:
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.assertRaises(ValueError):
                self.store.read("k", now=bad)
            with self.assertRaises(ValueError):
                self.store.destroyable(now=bad)

    def test_non_numeric_time_rejected(self) -> None:
        for bad in ("10", None, True, object()):
            with self.assertRaises(TypeError):
                self.store.read("k", now=bad)
            with self.assertRaises(TypeError):
                self.store.scan_expiry(now=bad)

    def test_empty_key_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.store.put_version("", "v1")

    def test_empty_version_id_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.store.put_version("k", "")

    def test_empty_hold_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.store.add_hold("k", "v1", "")
        with self.assertRaises(ValueError):
            self.store.put_version("other", "v1", holds=[""])

    def test_duplicate_holds_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.store.put_version("other", "v1", holds=["a", "a"])

    def test_negative_retention_is_legal_but_immediately_due(self) -> None:
        self.store.set_retention("k", "v1", -5)
        # ``now=0`` is already past the deadline of ``-5``.
        self.assertIsNone(self.store.removal_blocker("k", "v1", now=0))


class ErrorTypeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = VersionedStore()
        self.store.put_version("k", "v1")

    def test_unknown_key(self) -> None:
        with self.assertRaises(UnknownKeyError):
            self.store.versions("missing")
        with self.assertRaises(UnknownKeyError):
            self.store.remove_version("missing", "v1", now=0)
        with self.assertRaises(UnknownKeyError):
            self.store.snapshot("missing", now=0)

    def test_unknown_version(self) -> None:
        with self.assertRaises(UnknownVersionError):
            self.store.get_version("k", "nope")
        with self.assertRaises(UnknownVersionError):
            self.store.remove_version("k", "nope", now=0)

    def test_unknown_plan_and_batch(self) -> None:
        with self.assertRaises(UnknownPlanError):
            self.store.plan("plan-999999")
        with self.assertRaises(UnknownPlanError):
            self.store.execute_plan("plan-999999", now=0)
        plan = self.store.scan_expiry(now=0)
        with self.assertRaises(UnknownPlanError):
            self.store.execute_plan(plan, now=0, batch_ids=["bogus"])

    def test_unknown_receipt(self) -> None:
        self.assertIsNone(self.store.receipt("no-such-item"))
        with self.assertRaises(UnknownReceiptError):
            self.store.get_receipt("no-such-item")

    def test_error_hierarchy_is_value_error(self) -> None:
        self.assertTrue(issubclass(LifecycleError, ValueError))
        self.assertTrue(issubclass(UnknownKeyError, LifecycleError))
        self.assertTrue(issubclass(UnknownVersionError, LifecycleError))
        self.assertTrue(issubclass(UnknownPlanError, LifecycleError))
        self.assertTrue(issubclass(UnknownReceiptError, LifecycleError))
        self.assertTrue(issubclass(RetentionBlockedError, LifecycleError))


class MutationInvariantTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = VersionedStore()
        self.store.put_version("k", "v1", archived=True)

    def test_archive_live_version(self) -> None:
        self.store.put_version("k", "v2")
        record = self.store.archive("k", "v2")
        self.assertTrue(record.archived)
        self.store.unarchive("k", "v2")
        self.assertFalse(self.store.get_version("k", "v2").archived)

    def test_archive_delete_marker_rejected(self) -> None:
        self.store.put_delete_marker("k", "d1")
        with self.assertRaises(LifecycleError):
            self.store.archive("k", "d1")
        with self.assertRaises(LifecycleError):
            self.store.unarchive("k", "d1")

    def test_unarchive_live_version_rejected(self) -> None:
        self.store.put_version("k", "live")
        with self.assertRaises(LifecycleError):
            self.store.unarchive("k", "live")

    def test_archive_drops_existing_lease(self) -> None:
        self.store.grant_lease("k", "v1", "lease", granted_at=0, expires_at=10)
        record = self.store.archive("k", "v1")
        self.assertIsNone(record.lease)

    def test_revoke_lease(self) -> None:
        self.store.grant_lease("k", "v1", "lease", granted_at=0, expires_at=100)
        record = self.store.revoke_lease("k", "v1")
        self.assertIsNone(record.lease)
        self.assertIsNone(self.store.removal_blocker("k", "v1", now=5))

    def test_set_retention_on_delete_marker(self) -> None:
        self.store.put_delete_marker("k", "d1")
        record = self.store.set_retention("k", "d1", 50)
        self.assertEqual(record.retain_until, 50.0)
        self.assertEqual(
            self.store.removal_blocker("k", "d1", now=10),
            RetentionReason.RETENTION_NOT_REACHED,
        )

    def test_versions_reports_oldest_first(self) -> None:
        self.store.put_version("k", "v2")
        self.store.put_delete_marker("k", "d1")
        self.assertEqual(
            [r.version_id for r in self.store.versions("k")], ["v1", "v2", "d1"]
        )
        self.assertEqual(self.store.keys(), ("k",))


if __name__ == "__main__":
    unittest.main()
