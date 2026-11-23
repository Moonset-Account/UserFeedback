"""Legacy view: visibility, credential-based identity, rename, delete, merge."""

from __future__ import annotations

import unittest

from dualversion import ValidationError, WriteConflict

from .support import credential_for, legacy_by_name, new_store, spec_item


class LegacyVisibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = new_store()
        self.created = self.store.spec_add("api", "10.0.0.1:80", weight=42, drain=True)

    def test_legacy_items_expose_only_name_address_credential(self) -> None:
        legacy = self.store.legacy_get()
        self.assertEqual(set(legacy), {"resourceVersion", "items"})
        item = legacy["items"][0]
        self.assertEqual(set(item), {"name", "address", "credential"})
        self.assertEqual(item["name"], "api")
        self.assertEqual(item["address"], "10.0.0.1:80")

    def test_legacy_credential_is_opaque(self) -> None:
        credential = self.store.legacy_get()["items"][0]["credential"]
        self.assertTrue(credential.startswith("lv1."))
        self.assertNotIn("be-", credential)
        self.assertNotIn(self.created["id"], credential)

    def test_legacy_read_is_deterministic(self) -> None:
        self.assertEqual(self.store.legacy_get(), self.store.legacy_get())

    def test_credential_survives_invisible_changes(self) -> None:
        credential = credential_for(self.store, "api")
        backend_id = self.created["id"]
        self.store.spec_write(backend_id, {"weight": 7}, {"weight": 1})
        self.store.spec_write(backend_id, {"drain": False}, {"drain": 1})
        self.assertEqual(credential_for(self.store, "api"), credential)

    def test_credential_changes_when_visible_field_changes(self) -> None:
        credential = credential_for(self.store, "api")
        self.store.legacy_write(credential, {"address": "10.0.0.9:80"})
        self.assertNotEqual(credential_for(self.store, "api"), credential)


class LegacyWriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = new_store()
        self.created = self.store.spec_add("api", "10.0.0.1:80", weight=42, drain=True)

    def test_rename_keeps_identity_and_attributes(self) -> None:
        credential = credential_for(self.store, "api")
        result = self.store.legacy_write(credential, {"name": "api-v2"})
        self.assertEqual(result["status"], "renamed")
        self.assertEqual(result["previousName"], "api")
        item = spec_item(self.store, self.created["id"])
        self.assertEqual(item["name"], "api-v2")
        self.assertEqual(item["weight"], 42)
        self.assertTrue(item["drain"])
        self.assertEqual(legacy_by_name(self.store)["api-v2"]["address"], "10.0.0.1:80")

    def test_address_edit(self) -> None:
        credential = credential_for(self.store, "api")
        result = self.store.legacy_write(credential, {"address": "10.0.0.2:80"})
        self.assertEqual(result["status"], "updated")
        self.assertEqual(result["changed"], ["address"])
        self.assertEqual(spec_item(self.store, self.created["id"])["address"], "10.0.0.2:80")

    def test_noop_write_reports_unchanged(self) -> None:
        credential = credential_for(self.store, "api")
        result = self.store.legacy_write(
            credential, {"name": "api", "address": "10.0.0.1:80"}
        )
        self.assertEqual(result["status"], "unchanged")
        self.assertEqual(result["changed"], [])

    def test_stale_visible_write_conflicts(self) -> None:
        credential = credential_for(self.store, "api")
        self.store.legacy_write(credential, {"name": "api-v2"})
        with self.assertRaises(WriteConflict) as caught:
            self.store.legacy_write(credential, {"name": "api-v3"})
        self.assertEqual(caught.exception.reason, "field_modified")
        self.assertEqual(caught.exception.targets[0]["field"], "name")
        self.assertEqual(caught.exception.target_ids, (self.created["id"],))
        self.assertEqual(spec_item(self.store, self.created["id"])["name"], "api-v2")

    def test_two_legacy_writers_from_same_read_one_wins(self) -> None:
        credential = credential_for(self.store, "api")
        self.store.legacy_write(credential, {"address": "10.0.0.2:80"})
        with self.assertRaises(WriteConflict):
            self.store.legacy_write(credential, {"address": "10.0.0.3:80"})
        self.assertEqual(
            spec_item(self.store, self.created["id"])["address"], "10.0.0.2:80"
        )

    def test_merge_after_spec_changed_invisible_field(self) -> None:
        credential = credential_for(self.store, "api")
        backend_id = self.created["id"]
        self.store.spec_write(backend_id, {"weight": 5}, {"weight": 1})
        result = self.store.legacy_write(credential, {"name": "api-v2"})
        self.assertEqual(result["status"], "renamed")
        item = spec_item(self.store, backend_id)
        self.assertEqual(item["name"], "api-v2")
        self.assertEqual(item["weight"], 5)

    def test_legacy_write_ignores_stale_invisible_version(self) -> None:
        credential = credential_for(self.store, "api")
        backend_id = self.created["id"]
        self.store.spec_write(backend_id, {"drain": False}, {"drain": 1})
        # The credential encodes no drain version, so this still succeeds.
        result = self.store.legacy_write(credential, {"address": "10.0.0.9:80"})
        self.assertEqual(result["status"], "updated")
        self.assertFalse(spec_item(self.store, backend_id)["drain"])

    def test_rename_to_existing_name_rejected(self) -> None:
        self.store.spec_add("web", "10.0.0.2:80")
        credential = credential_for(self.store, "api")
        with self.assertRaises(ValidationError):
            self.store.legacy_write(credential, {"name": "web"})
        self.assertEqual(spec_item(self.store, self.created["id"])["name"], "api")

    def test_delete_by_credential_is_rename_safe(self) -> None:
        # Capture the credential, then rename the backend through the spec view.
        credential = credential_for(self.store, "api")
        self.store.spec_write(
            self.created["id"], {"name": "renamed"}, {"name": 1}
        )
        result = self.store.legacy_delete(credential)
        self.assertEqual(result["status"], "deleted")
        self.assertEqual(result["name"], "renamed")
        self.assertEqual(self.store.spec_get()["items"], [])

    def test_delete_missing_target_conflicts(self) -> None:
        credential = credential_for(self.store, "api")
        self.store.spec_delete(self.created["id"])
        with self.assertRaises(WriteConflict) as caught:
            self.store.legacy_delete(credential)
        self.assertEqual(caught.exception.reason, "target_deleted")
        self.assertEqual(caught.exception.target_ids, (self.created["id"],))

    def test_legacy_write_on_deleted_target_conflicts(self) -> None:
        credential = credential_for(self.store, "api")
        self.store.spec_delete(self.created["id"])
        with self.assertRaises(WriteConflict) as caught:
            self.store.legacy_write(credential, {"address": "1:1"})
        self.assertEqual(caught.exception.reason, "target_deleted")


if __name__ == "__main__":
    unittest.main()
