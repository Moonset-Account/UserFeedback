"""Boundary conditions and multi-instance credential behaviour."""

from __future__ import annotations

import unittest

from dualversion import BackendStore, ValidationError, WriteConflict

from .support import TEST_SECRET, by_name, credential_for, new_store


class EmptyAndBoundaryTests(unittest.TestCase):
    def test_empty_store_reads(self) -> None:
        store = new_store()
        self.assertEqual(store.legacy_get(), {"resourceVersion": 0, "items": []})
        snapshot = store.spec_get()
        self.assertEqual(snapshot, {"resourceVersion": 0, "items": [], "order": []})
        self.assertEqual(store.history(), [])

    def test_empty_commit_is_unchanged(self) -> None:
        store = new_store()
        result = store.legacy_commit([], replace=True)
        self.assertEqual(result["status"], "unchanged")
        self.assertFalse(result["changed"])
        self.assertEqual(result["resourceVersion"], 0)
        self.assertEqual(result["summary"]["totalItems"], 0)

    def test_empty_replace_prunes_everything(self) -> None:
        store = new_store()
        store.spec_add("a", "1:1")
        store.spec_add("b", "2:2")
        result = store.legacy_commit([], replace=True)
        self.assertEqual(by_name(store), {})
        self.assertEqual(result["summary"]["deletedPruned"], 2)

    def test_empty_merge_keeps_everything(self) -> None:
        store = new_store()
        store.spec_add("a", "1:1")
        result = store.legacy_commit([], replace=False)
        self.assertEqual(set(by_name(store)), {"a"})
        self.assertEqual(result["status"], "unchanged")

    def test_weight_boundaries(self) -> None:
        store = new_store()
        low = store.spec_add("low", "1:1", weight=0)
        high = store.spec_add("high", "2:2", weight=1000)
        self.assertEqual(low["item"]["weight"], 0)
        self.assertEqual(high["item"]["weight"], 1000)
        with self.assertRaises(ValidationError):
            store.spec_add("over", "3:3", weight=1001)
        with self.assertRaises(ValidationError):
            store.spec_add("under", "3:3", weight=-1)

    def test_name_and_address_are_trimmed(self) -> None:
        store = new_store()
        created = store.spec_add("  api  ", "  10.0.0.1:80  ")
        self.assertEqual(created["item"]["name"], "api")
        self.assertEqual(created["item"]["address"], "10.0.0.1:80")

    def test_unicode_names_round_trip(self) -> None:
        store = new_store()
        store.spec_add("后端-一", "10.0.0.1:80")
        self.assertEqual(store.legacy_get()["items"][0]["name"], "后端-一")
        credential = credential_for(store, "后端-一")
        store.legacy_write(credential, {"name": "后端-二"})
        self.assertEqual(set(by_name(store)), {"后端-二"})

    def test_delete_then_create_same_name_gets_new_id(self) -> None:
        store = new_store()
        first = store.spec_add("api", "1:1")
        store.spec_delete(first["id"])
        second = store.spec_add("api", "2:2")
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(by_name(store)["api"]["id"], second["id"])

    def test_legacy_commit_create_uses_defaults_for_invisible_fields(self) -> None:
        store = new_store()
        result = store.legacy_commit([{"name": "api", "address": "1:1"}])
        created_id = result["items"][0]["credential"]
        item = store.spec_get()["items"][0]
        self.assertEqual(item["weight"], 100)
        self.assertFalse(item["drain"])
        self.assertTrue(created_id.startswith("lv1."))

    def test_history_limit_is_enforced(self) -> None:
        store = new_store(history_limit=3)
        for index in range(6):
            store.spec_add(f"n{index}", f"10.0.0.{index}:80")
        self.assertEqual(len(store.history()), 3)


class SharedSecretTests(unittest.TestCase):
    def test_same_secret_credentials_decode_across_instances(self) -> None:
        store_a = BackendStore(secret=TEST_SECRET)
        store_b = BackendStore(secret=TEST_SECRET)
        store_a.spec_add("api", "1:1")
        credential = credential_for(store_a, "api")
        # The token is valid for store_b, but its id is unknown there: a
        # target_deleted conflict, not a malformed-credential error.
        with self.assertRaises(WriteConflict) as caught:
            store_b.legacy_write(credential, {"name": "x"})
        self.assertEqual(caught.exception.reason, "target_deleted")

    def test_default_secret_is_random_per_instance(self) -> None:
        store_a = BackendStore()
        store_b = BackendStore()
        store_a.spec_add("api", "1:1")
        credential = credential_for(store_a, "api")
        with self.assertRaises(ValidationError):
            store_b.legacy_write(credential, {"name": "x"})

    def test_credential_does_not_leak_between_instances_with_different_secrets(self) -> None:
        store_a = BackendStore(secret=b"store-a-secret-0123456789abcdef")
        store_b = BackendStore(secret=b"store-b-secret-0123456789abcdef")
        store_a.spec_add("api", "1:1")
        store_b.spec_add("api", "1:1")
        with self.assertRaises(ValidationError):
            store_b.legacy_write(credential_for(store_a, "api"), {"name": "x"})
        self.assertEqual(by_name(store_b)["api"]["address"], "1:1")


if __name__ == "__main__":
    unittest.main()
