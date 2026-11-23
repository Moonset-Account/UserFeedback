"""Name swap, identity binding, and the merge/replace commit semantics."""

from __future__ import annotations

import unittest

from dualversion import ValidationError, WriteConflict

from .support import by_name, credential_for, legacy_by_name, new_store, spec_item


class NameSwapTests(unittest.TestCase):
    """Swapping two names must not move canonical attributes between ids."""

    def setUp(self) -> None:
        self.store = new_store()
        self.alpha = self.store.spec_add("alpha", "10.0.0.1:80", weight=10)
        self.beta = self.store.spec_add("beta", "10.0.0.2:80", weight=900, drain=True)

    def test_swap_via_single_commit_keeps_weights_on_ids(self) -> None:
        items = [dict(item) for item in self.store.legacy_get()["items"]]
        self.assertEqual([item["name"] for item in items], ["alpha", "beta"])
        items[0]["name"], items[1]["name"] = items[1]["name"], items[0]["name"]
        result = self.store.legacy_commit(items)
        self.assertEqual(result["summary"]["renamed"], 2)
        self.assertEqual(result["status"], "applied")
        # Weight and drain travelled with the id, not the name.
        self.assertEqual(spec_item(self.store, self.alpha["id"])["name"], "beta")
        self.assertEqual(spec_item(self.store, self.alpha["id"])["weight"], 10)
        self.assertEqual(spec_item(self.store, self.beta["id"])["name"], "alpha")
        self.assertEqual(spec_item(self.store, self.beta["id"])["weight"], 900)
        self.assertTrue(spec_item(self.store, self.beta["id"])["drain"])

    def test_swap_via_legacy_list_only(self) -> None:
        # Mimic a client that only knows the legacy endpoint: read, swap names,
        # submit the whole list with credentials attached.
        legacy = self.store.legacy_get()["items"]
        swapped = [
            {
                "name": legacy[1]["name"],
                "address": legacy[0]["address"],
                "credential": legacy[0]["credential"],
            },
            {
                "name": legacy[0]["name"],
                "address": legacy[1]["address"],
                "credential": legacy[1]["credential"],
            },
        ]
        self.store.legacy_commit(swapped, replace=True)
        self.assertEqual(spec_item(self.store, self.alpha["id"])["address"], "10.0.0.1:80")
        self.assertEqual(spec_item(self.store, self.alpha["id"])["weight"], 10)
        self.assertEqual(spec_item(self.store, self.beta["id"])["address"], "10.0.0.2:80")
        self.assertEqual(spec_item(self.store, self.beta["id"])["weight"], 900)

    def test_swap_without_credentials_is_rejected_as_duplicate(self) -> None:
        with self.assertRaises(ValidationError):
            self.store.legacy_commit(
                [
                    {"name": "beta", "address": "10.0.0.1:80"},
                    {"name": "beta", "address": "10.0.0.2:80"},
                ]
            )

    def test_swap_then_stale_credential_edit_conflicts(self) -> None:
        credentials = {
            name: item["credential"]
            for name, item in legacy_by_name(self.store).items()
        }
        items = [dict(item) for item in self.store.legacy_get()["items"]]
        items[0]["name"], items[1]["name"] = items[1]["name"], items[0]["name"]
        self.store.legacy_commit(items)
        # Both credentials were consumed by the swap; reuse must conflict.
        with self.assertRaises(WriteConflict):
            self.store.legacy_write(credentials["alpha"], {"name": "gamma"})
        with self.assertRaises(WriteConflict):
            self.store.legacy_write(credentials["beta"], {"name": "delta"})


class CommitSemanticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = new_store()
        self.created = [
            self.store.spec_add(name, f"10.0.0.{index}:80", weight=10 * index)
            for index, name in enumerate(("one", "two", "three"), start=1)
        ]

    def test_commit_replaces_whole_list_and_prunes_omitted(self) -> None:
        legacy = self.store.legacy_get()["items"]
        kept = [
            {"name": "one", "address": "10.0.0.1:80", "credential": legacy[0]["credential"]},
            {"name": "new", "address": "10.0.0.9:80"},
        ]
        result = self.store.legacy_commit(kept, replace=True)
        self.assertEqual(set(by_name(self.store)), {"one", "new"})
        summary = result["summary"]
        self.assertEqual(summary["created"], 1)
        self.assertEqual(summary["deletedPruned"], 2)
        self.assertEqual(summary["totalDeleted"], 2)
        pruned_names = {
            entry["name"] for entry in result["deleted"] if entry["cause"] == "pruned"
        }
        self.assertEqual(pruned_names, {"two", "three"})

    def test_commit_merge_keeps_unlisted(self) -> None:
        result = self.store.legacy_commit(
            [{"name": "four", "address": "10.0.0.4:80"}], replace=False
        )
        self.assertEqual(set(by_name(self.store)), {"one", "two", "three", "four"})
        self.assertEqual(result["summary"]["created"], 1)
        self.assertEqual(result["summary"]["totalDeleted"], 0)

    def test_explicit_delete_by_credential(self) -> None:
        credential = credential_for(self.store, "two")
        result = self.store.legacy_commit([], [credential], replace=False)
        self.assertEqual(set(by_name(self.store)), {"one", "three"})
        self.assertEqual(result["summary"]["deletedExplicit"], 1)
        self.assertEqual(result["deleted"][0]["name"], "two")

    def test_explicit_delete_and_omit_is_not_double_counted(self) -> None:
        credential = credential_for(self.store, "two")
        legacy = self.store.legacy_get()["items"]
        kept = [
            {"name": item["name"], "address": item["address"], "credential": item["credential"]}
            for item in legacy
            if item["name"] != "two"
        ]
        result = self.store.legacy_commit(kept, [credential], replace=True)
        self.assertEqual(set(by_name(self.store)), {"one", "three"})
        self.assertEqual(result["summary"]["deletedExplicit"], 1)
        self.assertEqual(result["summary"]["deletedPruned"], 0)

    def test_order_is_rewritten_by_commit(self) -> None:
        legacy = self.store.legacy_get()["items"]
        reordered = [
            {"name": item["name"], "address": item["address"], "credential": item["credential"]}
            for item in reversed(legacy)
        ]
        result = self.store.legacy_commit(reordered, replace=True)
        self.assertTrue(result["orderChanged"])
        self.assertEqual(self.store.spec_get()["order"], [
            self.created[2]["id"],
            self.created[1]["id"],
            self.created[0]["id"],
        ])

    def test_commit_order_reporting(self) -> None:
        legacy = self.store.legacy_get()["items"]
        same_order = [
            {"name": item["name"], "address": item["address"], "credential": item["credential"]}
            for item in legacy
        ]
        result = self.store.legacy_commit(same_order, replace=True)
        self.assertFalse(result["orderChanged"])
        self.assertEqual(result["status"], "unchanged")
        self.assertFalse(result["changed"])

    def test_commit_rename_and_address_in_one_call(self) -> None:
        legacy = self.store.legacy_get()["items"]
        target = legacy[1]
        result = self.store.legacy_commit(
            [
                {
                    "name": "two-prime",
                    "address": "10.0.0.22:80",
                    "credential": target["credential"],
                }
            ],
            replace=False,
        )
        item = spec_item(self.store, self.created[1]["id"])
        self.assertEqual(item["name"], "two-prime")
        self.assertEqual(item["address"], "10.0.0.22:80")
        self.assertEqual(item["weight"], 20)
        self.assertEqual(result["items"][0]["status"], "renamed")

    def test_commit_merges_invisible_spec_changes(self) -> None:
        items = [dict(item) for item in self.store.legacy_get()["items"]]
        alpha = spec_item(self.store, self.created[0]["id"])
        # Spec changes weight and drain (invisible to legacy) after the read.
        self.store.spec_write(
            self.created[0]["id"], {"weight": 123}, {"weight": alpha["versions"]["weight"]}
        )
        self.store.spec_write(
            self.created[0]["id"], {"drain": True}, {"drain": alpha["versions"]["drain"]}
        )
        items[0]["address"] = "10.0.0.99:80"
        result = self.store.legacy_commit(items, replace=True)
        self.assertEqual(result["items"][0]["status"], "updated")
        after = spec_item(self.store, self.created[0]["id"])
        self.assertEqual(after["address"], "10.0.0.99:80")
        self.assertEqual(after["weight"], 123)
        self.assertTrue(after["drain"])

    def test_commit_with_stale_credential_is_rejected_atomically(self) -> None:
        stale_items = [dict(item) for item in self.store.legacy_get()["items"]]
        # Rename "two" through the spec view so the captured credential goes stale.
        self.store.spec_write(
            self.created[1]["id"], {"name": "two-x"}, {"name": 1}
        )
        # Prepare a commit that also changes "one"; it must not partially apply.
        offered = [
            {
                "name": item["name"],
                "address": "10.0.0.100:80" if item["name"] == "one" else item["address"],
                "credential": item["credential"],
            }
            for item in stale_items
        ]
        with self.assertRaises(WriteConflict) as caught:
            self.store.legacy_commit(offered, replace=True)
        self.assertEqual(caught.exception.reason, "field_modified")
        self.assertIn(self.created[1]["id"], caught.exception.target_ids)
        # "one" must be untouched because the batch failed.
        self.assertEqual(spec_item(self.store, self.created[0]["id"])["address"], "10.0.0.1:80")
        self.assertEqual(spec_item(self.store, self.created[1]["id"])["name"], "two-x")

    def test_commit_missing_target_is_atomic(self) -> None:
        credentials = {
            name: item["credential"]
            for name, item in legacy_by_name(self.store).items()
        }
        self.store.spec_delete(self.created[2]["id"])
        offered = [
            {"name": "one", "address": "10.0.0.11:80", "credential": credentials["one"]},
            {"name": "three", "address": "10.0.0.3:80", "credential": credentials["three"]},
        ]
        with self.assertRaises(WriteConflict) as caught:
            self.store.legacy_commit(offered, replace=True)
        self.assertEqual(caught.exception.reason, "target_deleted")
        self.assertEqual(caught.exception.target_ids, (self.created[2]["id"],))
        self.assertEqual(spec_item(self.store, self.created[0]["id"])["address"], "10.0.0.1:80")

    def test_commit_conflict_between_two_different_targets_is_mixed(self) -> None:
        credentials = {
            name: item["credential"]
            for name, item in legacy_by_name(self.store).items()
        }
        self.store.spec_write(self.created[0]["id"], {"name": "one-x"}, {"name": 1})
        self.store.spec_delete(self.created[2]["id"])
        offered = [
            {"name": "one", "address": "10.0.0.1:80", "credential": credentials["one"]},
            {"name": "three", "address": "10.0.0.3:80", "credential": credentials["three"]},
        ]
        with self.assertRaises(WriteConflict) as caught:
            self.store.legacy_commit(offered, replace=True)
        self.assertEqual(caught.exception.reason, "multiple_conflicts")
        self.assertEqual(
            caught.exception.target_ids, tuple(sorted([self.created[0]["id"], self.created[2]["id"]]))
        )

    def test_commit_cannot_submit_and_delete_same_backend(self) -> None:
        credential = credential_for(self.store, "one")
        with self.assertRaises(ValidationError):
            self.store.legacy_commit(
                [{"name": "one", "address": "10.0.0.1:80", "credential": credential}],
                [credential],
            )

    def test_commit_cannot_repeat_credential(self) -> None:
        credential = credential_for(self.store, "one")
        with self.assertRaises(ValidationError):
            self.store.legacy_commit(
                [
                    {"name": "one", "address": "10.0.0.1:80", "credential": credential},
                    {"name": "uno", "address": "10.0.0.1:80", "credential": credential},
                ]
            )


if __name__ == "__main__":
    unittest.main()
