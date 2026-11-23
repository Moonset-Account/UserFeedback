"""Interleaved concurrent scenarios: races, atomicity and replay determinism.

These tests drive callers through *interleavings* (read, someone else writes,
then the original caller writes) rather than relying on OS thread timing for
correctness.  A separate test exercises real threads to confirm the store's
lock keeps the invariant "at most one of two same-read writers wins".
"""

from __future__ import annotations

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from dualversion import ValidationError, WriteConflict

from .support import by_name, credential_for, legacy_by_name, new_store, spec_item


class InterleavedWriteTests(unittest.TestCase):
    """Both writers start from the same read; exactly one may succeed."""

    def setUp(self) -> None:
        self.store = new_store()
        self.created = self.store.spec_add("api", "10.0.0.1:80", weight=10)
        self.backend_id = self.created["id"]

    # -- spec view -------------------------------------------------------- #

    def test_two_spec_writers_same_field_one_wins(self) -> None:
        snapshot = spec_item(self.store, self.backend_id)
        version = snapshot["versions"]["weight"]
        first = self.store.spec_write(
            self.backend_id, {"weight": 20}, {"weight": version}
        )
        self.assertEqual(first["status"], "updated")
        with self.assertRaises(WriteConflict) as caught:
            self.store.spec_write(
                self.backend_id, {"weight": 30}, {"weight": version}
            )
        self.assertEqual(caught.exception.reason, "field_modified")
        self.assertEqual(caught.exception.target_ids, (self.backend_id,))
        self.assertEqual(spec_item(self.store, self.backend_id)["weight"], 20)

    def test_two_spec_writers_different_fields_both_win(self) -> None:
        snapshot = spec_item(self.store, self.backend_id)
        versions = snapshot["versions"]
        self.store.spec_write(self.backend_id, {"weight": 20}, {"weight": versions["weight"]})
        # The name version is unchanged, so a concurrent name edit still works.
        result = self.store.spec_write(
            self.backend_id, {"name": "api2"}, {"name": versions["name"]}
        )
        self.assertEqual(result["status"], "updated")
        item = spec_item(self.store, self.backend_id)
        self.assertEqual(item["name"], "api2")
        self.assertEqual(item["weight"], 20)

    # -- one spec writer, one legacy writer ------------------------------ #

    def test_spec_and_legacy_edit_same_visible_field(self) -> None:
        credential = credential_for(self.store, "api")
        versions = spec_item(self.store, self.backend_id)["versions"]
        self.store.spec_write(self.backend_id, {"address": "10.0.0.5:80"}, {"address": versions["address"]})
        with self.assertRaises(WriteConflict) as caught:
            self.store.legacy_write(credential, {"address": "10.0.0.6:80"})
        self.assertEqual(caught.exception.reason, "field_modified")
        self.assertEqual(caught.exception.targets[0]["field"], "address")
        self.assertEqual(spec_item(self.store, self.backend_id)["address"], "10.0.0.5:80")

    def test_spec_edit_invisible_then_legacy_edit_merges(self) -> None:
        credential = credential_for(self.store, "api")
        versions = spec_item(self.store, self.backend_id)["versions"]
        self.store.spec_write(self.backend_id, {"weight": 77}, {"weight": versions["weight"]})
        result = self.store.legacy_write(credential, {"address": "10.0.0.7:80"})
        self.assertEqual(result["status"], "updated")
        item = spec_item(self.store, self.backend_id)
        self.assertEqual(item["weight"], 77)
        self.assertEqual(item["address"], "10.0.0.7:80")

    def test_legacy_edit_then_spec_write_with_fresh_version(self) -> None:
        credential = credential_for(self.store, "api")
        self.store.legacy_write(credential, {"name": "api-v2"})
        fresh = spec_item(self.store, self.backend_id)
        result = self.store.spec_write(
            self.backend_id, {"name": "api-v3"}, {"name": fresh["versions"]["name"]}
        )
        self.assertEqual(result["status"], "updated")
        self.assertEqual(spec_item(self.store, self.backend_id)["name"], "api-v3")

    # -- deletion interleavings ------------------------------------------ #

    def test_delete_then_concurrent_edits_conflict(self) -> None:
        credential = credential_for(self.store, "api")
        versions = spec_item(self.store, self.backend_id)["versions"]
        self.store.spec_delete(self.backend_id)
        with self.assertRaises(WriteConflict) as legacy_caught:
            self.store.legacy_write(credential, {"name": "x"})
        self.assertEqual(legacy_caught.exception.reason, "target_deleted")
        with self.assertRaises(WriteConflict) as spec_caught:
            self.store.spec_write(self.backend_id, {"weight": 2}, {"weight": versions["weight"]})
        self.assertEqual(spec_caught.exception.reason, "target_deleted")

    def test_two_deletes_one_conflicts(self) -> None:
        self.store.spec_delete(self.backend_id)
        with self.assertRaises(WriteConflict) as caught:
            self.store.spec_delete(self.backend_id)
        self.assertEqual(caught.exception.reason, "target_deleted")

    def test_delete_during_commit_is_atomic(self) -> None:
        self.store.spec_add("web", "10.0.0.2:80")
        offered = [dict(item) for item in self.store.legacy_get()["items"]]
        self.store.spec_delete(self.backend_id)
        with self.assertRaises(WriteConflict):
            self.store.legacy_commit(offered, replace=True)
        # "web" survives because the whole commit was rolled back.
        self.assertEqual(set(by_name(self.store)), {"web"})


class RealThreadRaceTests(unittest.TestCase):
    def test_concurrent_spec_writers_single_winner(self) -> None:
        store = new_store()
        created = store.spec_add("api", "10.0.0.1:80")
        backend_id = created["id"]
        version = created["item"]["versions"]["weight"]
        barrier = threading.Barrier(8)

        def attempt(weight: int) -> str:
            barrier.wait()
            try:
                store.spec_write(backend_id, {"weight": weight}, {"weight": version})
                return "ok"
            except WriteConflict:
                return "conflict"

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(attempt, range(8)))
        self.assertEqual(results.count("ok"), 1)
        self.assertEqual(results.count("conflict"), 7)
        winner = spec_item(store, backend_id)
        self.assertEqual(winner["versions"]["weight"], 2)

    def test_concurrent_legacy_writers_single_winner(self) -> None:
        store = new_store()
        store.spec_add("api", "10.0.0.1:80")
        credential = credential_for(store, "api")
        barrier = threading.Barrier(6)

        def attempt(index: int) -> str:
            barrier.wait()
            try:
                store.legacy_write(credential, {"address": f"10.0.0.{index + 10}:80"})
                return "ok"
            except WriteConflict:
                return "conflict"

        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(attempt, range(6)))
        self.assertEqual(results.count("ok"), 1)
        legacy = legacy_by_name(store)["api"]
        self.assertIn(legacy["address"], {f"10.0.0.{index + 10}:80" for index in range(6)})


class AtomicBatchTests(unittest.TestCase):
    def test_batch_with_one_bad_name_creates_nothing(self) -> None:
        store = new_store()
        store.spec_add("existing", "10.0.0.1:80")
        with self.assertRaises(ValidationError):
            store.legacy_commit(
                [
                    {"name": "fresh", "address": "10.0.0.2:80"},
                    {"name": "existing", "address": "10.0.0.3:80"},
                ],
                replace=False,
            )
        self.assertEqual(set(by_name(store)), {"existing"})
        self.assertEqual(store.resource_version, 1)

    def test_batch_with_bad_credential_creates_nothing(self) -> None:
        store = new_store()
        store.spec_add("existing", "10.0.0.1:80")
        with self.assertRaises(ValidationError):
            store.legacy_commit(
                [
                    {"name": "fresh", "address": "10.0.0.2:80"},
                    {"name": "other", "address": "10.0.0.3:80", "credential": "lv1.bad.bad"},
                ],
                replace=False,
            )
        self.assertEqual(set(by_name(store)), {"existing"})
        self.assertEqual(store.resource_version, 1)

    def test_resource_version_not_bumped_by_failed_batch(self) -> None:
        store = new_store()
        store.spec_add("existing", "10.0.0.1:80")
        before = store.resource_version
        with self.assertRaises(ValidationError):
            store.legacy_commit([{"name": "existing", "address": "9:9"}], replace=False)
        self.assertEqual(store.resource_version, before)


if __name__ == "__main__":
    unittest.main()
