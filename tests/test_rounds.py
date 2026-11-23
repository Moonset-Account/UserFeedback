"""End-to-end multi-round scenario across both versions.

This mirrors the acceptance criterion: repeatedly read in one version, write
back in the other and replay requests, while observing per-item preserved,
overwritten and deleted outcomes.
"""

from __future__ import annotations

import unittest

from dualversion import ValidationError, WriteConflict

from .support import by_name, credential_for, legacy_by_name, new_store, spec_item


class MultiRoundScenarioTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = new_store()
        self.alpha = self.store.spec_add("alpha", "10.0.0.1:80", weight=10)
        self.beta = self.store.spec_add("beta", "10.0.0.2:80", weight=20, drain=True)
        self.gamma = self.store.spec_add("gamma", "10.0.0.3:80", weight=30)

    def test_full_scenario_round_by_round(self) -> None:
        # Round 1: legacy read; no writes.
        round1 = self.store.legacy_get()
        self.assertEqual(
            [item["name"] for item in round1["items"]], ["alpha", "beta", "gamma"]
        )
        credentials = {
            item["name"]: item["credential"] for item in round1["items"]
        }

        # Round 2: spec overwrites invisible fields on alpha and beta.
        alpha_invisible = spec_item(self.store, self.alpha["id"])["versions"]
        beta_invisible = spec_item(self.store, self.beta["id"])["versions"]
        self.store.spec_write(
            self.alpha["id"], {"weight": 11}, {"weight": alpha_invisible["weight"]}
        )
        self.store.spec_write(
            self.beta["id"], {"drain": False}, {"drain": beta_invisible["drain"]}
        )

        # Round 3: legacy renames alpha and edits beta's address, using the
        # credentials from round 1 -- both must merge over the invisible edits.
        alpha_result = self.store.legacy_write(credentials["alpha"], {"name": "alpha-2"})
        beta_result = self.store.legacy_write(
            credentials["beta"], {"address": "10.0.0.22:80"}
        )
        self.assertEqual(alpha_result["status"], "renamed")
        self.assertEqual(beta_result["status"], "updated")
        self.assertEqual(spec_item(self.store, self.alpha["id"])["weight"], 11)
        self.assertFalse(spec_item(self.store, self.beta["id"])["drain"])

        # Round 4: legacy read shows the merged result; only visible fields.
        round4 = legacy_by_name(self.store)
        self.assertEqual(set(round4), {"alpha-2", "beta", "gamma"})
        self.assertEqual(round4["beta"]["address"], "10.0.0.22:80")

        # Round 5: replaying the round-3 request is a value-level no-op. The
        # credential is stale, but since the requested values already hold,
        # nothing needs to change and nothing conflicts.
        replay = self.store.legacy_write(credentials["alpha"], {"name": "alpha-2"})
        self.assertEqual(replay["status"], "unchanged")
        self.assertEqual(replay["changed"], [])
        # A *different* change with that stale credential does conflict.
        with self.assertRaises(WriteConflict) as caught:
            self.store.legacy_write(credentials["alpha"], {"name": "alpha-3"})
        self.assertEqual(caught.exception.reason, "field_modified")

        # Round 6: spec reads, deletes gamma, and writes a new visible field.
        self.store.spec_delete(self.gamma["id"])
        alpha_now = spec_item(self.store, self.alpha["id"])["versions"]
        self.store.spec_write(
            self.alpha["id"], {"address": "10.0.0.111:80"}, {"address": alpha_now["address"]}
        )

        # Round 7: a legacy read + full-list commit sees deletion of gamma only
        # after it observes it. This stale list carries two independent
        # problems -- gamma is gone and alpha-2's address moved in round 6 --
        # so the batch fails as a mixed conflict with both ids named.
        stale_list = [
            {
                "name": name,
                "address": item["address"],
                "credential": item["credential"],
            }
            for name, item in round4.items()
        ]
        with self.assertRaises(WriteConflict) as stale_caught:
            self.store.legacy_commit(stale_list, replace=True)
        self.assertEqual(stale_caught.exception.reason, "multiple_conflicts")
        self.assertEqual(
            stale_caught.exception.target_ids,
            tuple(sorted([self.gamma["id"], self.alpha["id"]])),
        )
        reasons = {
            (target["id"], target["reason"])
            for target in stale_caught.exception.targets
        }
        self.assertIn((self.gamma["id"], "target_deleted"), reasons)
        self.assertIn((self.alpha["id"], "field_modified"), reasons)
        # Atomic: nothing from the stale batch was applied.
        self.assertEqual(
            set(by_name(self.store)), {"alpha-2", "beta"}
        )

        # Round 8: fresh read; reorder + rename + create + explicit delete in one
        # atomic commit, then verify each item's outcome individually.
        fresh = self.store.legacy_get()["items"]
        beta_credential = next(
            item["credential"] for item in fresh if item["name"] == "beta"
        )
        result = self.store.legacy_commit(
            [
                {
                    "name": "beta-renamed",
                    "address": "10.0.0.22:80",
                    "credential": beta_credential,
                },
                {
                    "name": "alpha-2",
                    "address": "10.0.0.111:80",
                    "credential": next(
                        item["credential"] for item in fresh if item["name"] == "alpha-2"
                    ),
                },
                {"name": "delta", "address": "10.0.0.4:80"},
            ],
            replace=True,
        )
        statuses = {item["name"]: item["status"] for item in result["items"]}
        self.assertEqual(
            statuses, {"beta-renamed": "renamed", "alpha-2": "unchanged", "delta": "created"}
        )
        self.assertTrue(result["orderChanged"])
        self.assertEqual(result["summary"]["created"], 1)
        self.assertEqual(result["summary"]["renamed"], 1)
        self.assertEqual(result["summary"]["unchanged"], 1)

        # Final canonical truth: ids preserved where the credential matched,
        # attributes retained, new item has defaults, order follows submission.
        final = self.store.spec_get()
        self.assertEqual(
            [item["name"] for item in final["items"]],
            ["beta-renamed", "alpha-2", "delta"],
        )
        self.assertEqual(final["order"][0], self.beta["id"])
        self.assertEqual(final["order"][1], self.alpha["id"])
        self.assertEqual(spec_item(self.store, self.beta["id"])["weight"], 20)
        self.assertFalse(spec_item(self.store, self.beta["id"])["drain"])
        self.assertEqual(spec_item(self.store, self.alpha["id"])["weight"], 11)
        delta = by_name(self.store)["delta"]
        self.assertEqual(delta["weight"], 100)
        self.assertFalse(delta["drain"])

    def test_replay_after_success_is_stable_and_reported(self) -> None:
        payload = [
            {"name": "alpha", "address": "10.0.0.1:80", "credential": credential_for(self.store, "alpha")},
            {"name": "beta", "address": "10.0.0.2:80", "credential": credential_for(self.store, "beta")},
            {"name": "gamma", "address": "10.0.0.3:80", "credential": credential_for(self.store, "gamma")},
        ]
        first = self.store.legacy_commit(payload, replace=True, idempotency_key="sync")
        self.assertEqual(first["status"], "unchanged")
        replayed = self.store.legacy_commit(payload, replace=True, idempotency_key="sync")
        self.assertTrue(replayed["replayed"])
        self.assertEqual(replayed["items"], first["items"])
        self.assertEqual(replayed["summary"], first["summary"])
        self.assertEqual(self.store.resource_version, 3)

    def test_item_with_only_invisible_changes_is_unchanged_legacy(self) -> None:
        before = credential_for(self.store, "alpha")
        versions = spec_item(self.store, self.alpha["id"])["versions"]
        self.store.spec_write(self.alpha["id"], {"weight": 5}, {"weight": versions["weight"]})
        legacy = legacy_by_name(self.store)["alpha"]
        self.assertEqual(legacy["name"], "alpha")
        self.assertEqual(legacy["address"], "10.0.0.1:80")
        self.assertEqual(legacy["credential"], before)

    def test_address_only_change_then_stale_name_change_conflicts(self) -> None:
        credential = credential_for(self.store, "alpha")
        self.store.spec_write(
            self.alpha["id"], {"name": "alpha-x"}, {"name": 1}
        )
        # Credential's address version still matches; only name is stale.
        with self.assertRaises(WriteConflict) as caught:
            self.store.legacy_write(credential, {"name": "alpha-y"})
        self.assertEqual(
            [target["field"] for target in caught.exception.targets], ["name"]
        )


if __name__ == "__main__":
    unittest.main()
