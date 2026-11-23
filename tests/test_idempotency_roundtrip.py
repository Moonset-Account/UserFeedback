"""Idempotent replay and read/write/replay round-trip behaviour."""

from __future__ import annotations

import unittest

from dualversion import ValidationError, WriteConflict

from .support import by_name, credential_for, legacy_by_name, new_store, spec_item


class IdempotencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = new_store()

    def test_repeated_add_returns_same_backend(self) -> None:
        first = self.store.spec_add("api", "10.0.0.1:80", idempotency_key="add-1")
        second = self.store.spec_add("api", "10.0.0.1:80", idempotency_key="add-1")
        self.assertEqual(first["id"], second["id"])
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(len(self.store.spec_get()["items"]), 1)

    def test_reused_key_with_different_payload_conflicts(self) -> None:
        self.store.spec_add("api", "10.0.0.1:80", idempotency_key="add-1")
        with self.assertRaises(WriteConflict) as caught:
            self.store.spec_add("other", "10.0.0.2:80", idempotency_key="add-1")
        self.assertEqual(caught.exception.reason, "idempotency_key_reuse")
        self.assertEqual(set(by_name(self.store)), {"api"})

    def test_replayed_write_does_not_apply_twice(self) -> None:
        created = self.store.spec_add("api", "10.0.0.1:80")
        backend_id = created["id"]
        first = self.store.spec_write(
            backend_id, {"weight": 5}, {"weight": 1}, idempotency_key="w-1"
        )
        replayed = self.store.spec_write(
            backend_id, {"weight": 5}, {"weight": 1}, idempotency_key="w-1"
        )
        self.assertTrue(replayed["replayed"])
        self.assertEqual(replayed["resourceVersion"], first["resourceVersion"])
        self.assertEqual(spec_item(self.store, backend_id)["versions"]["weight"], 2)
        self.assertEqual(self.store.resource_version, 2)

    def test_replayed_delete_after_gone_succeeds(self) -> None:
        created = self.store.spec_add("api", "10.0.0.1:80")
        self.store.spec_delete(created["id"], idempotency_key="d-1")
        replayed = self.store.spec_delete(created["id"], idempotency_key="d-1")
        self.assertEqual(replayed["status"], "deleted")
        self.assertTrue(replayed["replayed"])

    def test_replayed_legacy_delete_after_gone_succeeds(self) -> None:
        self.store.spec_add("api", "10.0.0.1:80")
        credential = credential_for(self.store, "api")
        self.store.legacy_delete(credential, idempotency_key="ld-1")
        replayed = self.store.legacy_delete(credential, idempotency_key="ld-1")
        self.assertTrue(replayed["replayed"])
        self.assertEqual(replayed["status"], "deleted")

    def test_replayed_commit_does_not_rerun(self) -> None:
        self.store.spec_add("one", "10.0.0.1:80")
        first = self.store.legacy_commit(
            [{"name": "two", "address": "10.0.0.2:80"}],
            replace=False,
            idempotency_key="c-1",
        )
        replayed = self.store.legacy_commit(
            [{"name": "two", "address": "10.0.0.2:80"}],
            replace=False,
            idempotency_key="c-1",
        )
        self.assertTrue(replayed["replayed"])
        self.assertEqual(replayed["items"], first["items"])
        self.assertEqual(len(self.store.spec_get()["items"]), 2)

    def test_unkeyed_add_with_same_payload_creates_distinct_backends(self) -> None:
        first = self.store.spec_add("api", "10.0.0.1:80")
        with self.assertRaises(ValidationError):
            # Names must stay unique, so this is a validation error rather than
            # a silent duplicate.
            self.store.spec_add("api", "10.0.0.1:80")
        self.assertEqual(len(self.store.spec_get()["items"]), 1)
        self.assertEqual(self.store.spec_get()["items"][0]["id"], first["id"])

    def test_failed_conflict_does_not_poison_key(self) -> None:
        created = self.store.spec_add("api", "10.0.0.1:80")
        backend_id = created["id"]
        with self.assertRaises(WriteConflict):
            self.store.spec_write(
                backend_id, {"weight": 5}, {"weight": 99}, idempotency_key="w-2"
            )
        # A later request with a correct version and the same key can succeed.
        result = self.store.spec_write(
            backend_id, {"weight": 5}, {"weight": 1}, idempotency_key="w-2"
        )
        self.assertEqual(result["status"], "updated")
        self.assertFalse(result["replayed"])


class RoundTripTests(unittest.TestCase):
    """Read in one version, write in the other, and verify observable effects."""

    def setUp(self) -> None:
        self.store = new_store()
        self.created = self.store.spec_add("api", "10.0.0.1:80", weight=42, drain=True)

    def test_spec_read_then_legacy_edit_then_spec_read(self) -> None:
        before = self.store.spec_get()["items"][0]
        legacy_item = self.store.legacy_get()["items"][0]
        self.store.legacy_write(legacy_item["credential"], {"name": "api-renamed"})
        after = self.store.spec_get()["items"][0]
        self.assertEqual(after["id"], before["id"])
        self.assertEqual(after["name"], "api-renamed")
        self.assertEqual(after["weight"], before["weight"])
        self.assertEqual(after["drain"], before["drain"])
        self.assertEqual(after["address"], before["address"])

    def test_legacy_read_then_spec_edit_then_legacy_read(self) -> None:
        credential_before = credential_for(self.store, "api")
        backend_id = self.created["id"]
        self.store.spec_write(backend_id, {"weight": 7}, {"weight": 1})
        self.store.spec_write(backend_id, {"address": "10.0.0.77:80"}, {"address": 1})
        legacy = legacy_by_name(self.store)["api"]
        self.assertEqual(legacy["address"], "10.0.0.77:80")
        self.assertNotEqual(legacy["credential"], credential_before)

    def test_repeated_read_write_cycles_converge(self) -> None:
        for iteration in range(5):
            credential = credential_for(self.store, "api")
            desired = f"10.0.0.{iteration + 20}:80"
            self.store.legacy_write(credential, {"address": desired})
            self.assertEqual(legacy_by_name(self.store)["api"]["address"], desired)
        item = spec_item(self.store, self.created["id"])
        self.assertEqual(item["address"], "10.0.0.24:80")
        self.assertEqual(item["weight"], 42)
        self.assertTrue(item["drain"])

    def test_history_lists_applied_mutations_in_order(self) -> None:
        credential = credential_for(self.store, "api")
        self.store.legacy_write(credential, {"name": "api2"})
        entries = self.store.history()
        operations = [entry["operation"] for entry in entries]
        self.assertEqual(operations[0], "spec_add")
        self.assertIn("legacy_write", operations)
        versions = [entry["resourceVersion"] for entry in entries]
        self.assertEqual(versions, sorted(versions))
        after_first = self.store.history(since_resource_version=1)
        self.assertTrue(all(entry["resourceVersion"] > 1 for entry in after_first))

    def test_history_excludes_replays(self) -> None:
        baseline = sum(
            1 for entry in self.store.history() if entry["operation"] == "spec_add"
        )
        self.store.spec_add("x", "1:1", idempotency_key="h-1")
        self.store.spec_add("x", "1:1", idempotency_key="h-1")
        added = [
            entry for entry in self.store.history() if entry["operation"] == "spec_add"
        ]
        self.assertEqual(len(added), baseline + 1)


if __name__ == "__main__":
    unittest.main()
