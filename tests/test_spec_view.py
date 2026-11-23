"""Canonical (spec) view: create, read, conditional write, delete."""

from __future__ import annotations

import unittest

from dualversion import ValidationError, WriteConflict

from .support import new_store, spec_item


class SpecCrudTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = new_store()

    def test_add_assigns_stable_id_and_defaults(self) -> None:
        created = self.store.spec_add("api", "10.0.0.1:80")
        self.assertEqual(created["status"], "created")
        self.assertTrue(created["id"].startswith("be-"))
        item = created["item"]
        self.assertEqual(item["weight"], 100)
        self.assertFalse(item["drain"])
        self.assertEqual(
            item["versions"], {"name": 1, "address": 1, "weight": 1, "drain": 1}
        )

    def test_ids_are_unique_and_never_reused(self) -> None:
        ids = {
            self.store.spec_add(f"n{index}", f"10.0.0.{index}:80")["id"]
            for index in range(20)
        }
        self.assertEqual(len(ids), 20)

    def test_read_all_and_single(self) -> None:
        first = self.store.spec_add("api", "10.0.0.1:80")
        second = self.store.spec_add("web", "10.0.0.2:80")
        everything = self.store.spec_get()
        self.assertEqual(everything["order"], [first["id"], second["id"]])
        self.assertEqual(
            [item["id"] for item in everything["items"]],
            [first["id"], second["id"]],
        )
        single = self.store.spec_get(second["id"])
        self.assertEqual(single["items"][0]["name"], "web")
        with self.assertRaises(KeyError):
            self.store.spec_get("be-missing")

    def test_duplicate_name_rejected(self) -> None:
        self.store.spec_add("api", "10.0.0.1:80")
        with self.assertRaises(ValidationError):
            self.store.spec_add("api", "10.0.0.2:80")
        self.assertEqual(len(self.store.spec_get()["items"]), 1)

    def test_conditional_weight_update(self) -> None:
        created = self.store.spec_add("api", "10.0.0.1:80")
        updated = self.store.spec_write(
            created["id"], {"weight": 5}, {"weight": 1}
        )
        self.assertEqual(updated["status"], "updated")
        self.assertEqual(updated["changed"], ["weight"])
        self.assertEqual(updated["item"]["versions"]["weight"], 2)
        self.assertEqual(updated["item"]["versions"]["name"], 1)

    def test_unrelated_version_change_does_not_block_update(self) -> None:
        created = self.store.spec_add("api", "10.0.0.1:80")
        backend_id = created["id"]
        self.store.spec_write(backend_id, {"weight": 5}, {"weight": 1})
        # name version is still 1; only weight was bumped.
        result = self.store.spec_write(
            backend_id, {"name": "api2"}, {"name": 1, "weight": 1}
        )
        self.assertEqual(result["changed"], ["name"])

    def test_stale_version_conflicts_and_does_not_mutate(self) -> None:
        created = self.store.spec_add("api", "10.0.0.1:80")
        backend_id = created["id"]
        self.store.spec_write(backend_id, {"weight": 5}, {"weight": 1})
        with self.assertRaises(WriteConflict) as caught:
            self.store.spec_write(backend_id, {"weight": 7}, {"weight": 1})
        error = caught.exception
        self.assertEqual(error.reason, "field_modified")
        self.assertEqual(error.target_ids, (backend_id,))
        self.assertEqual(error.targets[0]["field"], "weight")
        item = spec_item(self.store, backend_id)
        self.assertEqual(item["weight"], 5)

    def test_multi_field_write_conflict_reports_every_field(self) -> None:
        created = self.store.spec_add("api", "10.0.0.1:80")
        backend_id = created["id"]
        self.store.spec_write(backend_id, {"address": "9.9.9.9:9"}, {"address": 1})
        self.store.spec_write(backend_id, {"weight": 3}, {"weight": 1})
        with self.assertRaises(WriteConflict) as caught:
            self.store.spec_write(
                backend_id,
                {"address": "8.8.8.8:8", "weight": 4},
                {"address": 1, "weight": 1},
            )
        fields = sorted(target["field"] for target in caught.exception.targets)
        self.assertEqual(fields, ["address", "weight"])

    def test_only_stale_fields_are_reported(self) -> None:
        created = self.store.spec_add("api", "10.0.0.1:80")
        backend_id = created["id"]
        self.store.spec_write(backend_id, {"weight": 5}, {"weight": 1})
        with self.assertRaises(WriteConflict) as caught:
            self.store.spec_write(
                backend_id,
                {"name": "api2", "weight": 8},
                {"name": 1, "weight": 1},
            )
        self.assertEqual(
            [target["field"] for target in caught.exception.targets], ["weight"]
        )
        # Nothing applied, including the field whose version was correct.
        item = spec_item(self.store, backend_id)
        self.assertEqual(item["name"], "api")
        self.assertEqual(item["weight"], 5)

    def test_force_bypasses_version_check(self) -> None:
        created = self.store.spec_add("api", "10.0.0.1:80")
        backend_id = created["id"]
        self.store.spec_write(backend_id, {"weight": 5}, {"weight": 1})
        result = self.store.spec_write(backend_id, {"weight": 6}, force=True)
        self.assertEqual(result["status"], "updated")
        self.assertEqual(result["item"]["weight"], 6)

    def test_delete_removes_item_and_reports_snapshot(self) -> None:
        created = self.store.spec_add("api", "10.0.0.1:80")
        deleted = self.store.spec_delete(created["id"])
        self.assertEqual(deleted["status"], "deleted")
        self.assertEqual(deleted["item"]["name"], "api")
        self.assertEqual(self.store.spec_get()["items"], [])
        with self.assertRaises(KeyError):
            self.store.spec_get(created["id"])

    def test_delete_with_stale_version_conflicts(self) -> None:
        created = self.store.spec_add("api", "10.0.0.1:80")
        backend_id = created["id"]
        self.store.spec_write(backend_id, {"weight": 5}, {"weight": 1})
        with self.assertRaises(WriteConflict) as caught:
            self.store.spec_delete(backend_id, {"weight": 1})
        self.assertEqual(caught.exception.reason, "field_modified")
        self.assertIsNotNone(self.store.spec_get(backend_id))

    def test_delete_missing_target_reports_target_deleted(self) -> None:
        with self.assertRaises(WriteConflict) as caught:
            self.store.spec_delete("be-ghost")
        self.assertEqual(caught.exception.reason, "target_deleted")
        self.assertEqual(caught.exception.target_ids, ("be-ghost",))

    def test_write_missing_target_reports_target_deleted(self) -> None:
        with self.assertRaises(WriteConflict) as caught:
            self.store.spec_write("be-ghost", {"weight": 2}, {"weight": 1})
        self.assertEqual(caught.exception.reason, "target_deleted")

    def test_rename_to_taken_name_rejected(self) -> None:
        first = self.store.spec_add("api", "10.0.0.1:80")
        self.store.spec_add("web", "10.0.0.2:80")
        with self.assertRaises(ValidationError):
            self.store.spec_write(first["id"], {"name": "web"}, {"name": 1})
        self.assertEqual(spec_item(self.store, first["id"])["name"], "api")

    def test_read_snapshots_are_independent_copies(self) -> None:
        created = self.store.spec_add("api", "10.0.0.1:80")
        snapshot = self.store.spec_get()["items"][0]
        snapshot["weight"] = 999
        self.assertEqual(spec_item(self.store, created["id"])["weight"], 100)


if __name__ == "__main__":
    unittest.main()
