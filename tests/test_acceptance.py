"""Acceptance tests for the login-name migration planner.

Every test drives the real public API and asserts on the resulting plan or on
the post-apply lookup state; there are no stubs.
"""

import unittest
from datetime import date, timedelta

from login_migration import (
    Account,
    Alias,
    Conflict,
    Group,
    MigrationError,
    apply_plan,
    plan_migration,
)
from login_migration.migrator import (
    COMPONENT_DEFERRED,
    DUPLICATE_TARGET_KEY,
    OWNED_BY_OTHER_ACCOUNT,
    PREEXISTING_COLLISION,
    RESERVED_BY_ALIAS,
)

ON = date(2026, 11, 20)
LATER = ON + timedelta(days=30)
EARLIER = ON - timedelta(days=1)


def old_rule(name: str) -> str:
    """Legacy rule: ASCII-lowercase only, no Unicode folding."""
    return name.lower()


def new_rule(name: str) -> str:
    """New rule: full Unicode case folding."""
    return name.casefold()


def plan(accounts, groups, on=ON):
    return plan_migration(
        accounts,
        groups,
        old_normalize=old_rule,
        new_normalize=new_rule,
        on=on,
    )


class BasicRenameTests(unittest.TestCase):
    def test_single_rename_is_executed_and_findable_by_new_name(self):
        accounts = [Account(1, "Alice"), Account(2, "Bob")]
        groups = [Group("g1", (1,), {1: "alice"})]

        preview = plan(accounts, groups)

        self.assertEqual(preview.renames, {1: "alice"})
        self.assertEqual(preview.conflicts, ())
        self.assertEqual(preview.deferred, ())

        state = apply_plan(preview)
        self.assertEqual(state.find("ALICE").account_id, 1)
        self.assertEqual(state.find("alice").account_id, 1)
        self.assertEqual(state.primary_of(1), "alice")

    def test_old_rule_and_new_rule_are_injectable(self):
        accounts = [Account(1, "Straße")]
        groups = [Group("g", (1,), {1: "STRASSE"})]

        # Legacy lower() maps "Straße" to "straße", so no legacy clash.
        preview = plan(accounts, groups)
        self.assertEqual(preview.renames, {1: "STRASSE"})
        # casefold maps both spellings to "strasse".
        self.assertEqual(apply_plan(preview).find("straße").account_id, 1)

    def test_unicode_merge_under_new_rule_but_not_old(self):
        # "K" (Kelvin sign) casefolds to "k" but lowercases to "k" too; use
        # sigma where the final form folds.
        accounts = [Account(7, "ΣΟΦΙΑ")]
        groups = [Group("g", (7,), {7: "σοφια"})]
        preview = plan(accounts, groups)
        self.assertEqual(preview.renames, {7: "σοφια"})
        self.assertEqual(apply_plan(preview).find("ΣΟΦΙΑ").account_id, 7)

    def test_account_untouched_when_not_in_any_group(self):
        preview = plan([Account(1, "Alice")], [])
        self.assertEqual(preview.renames, {})
        self.assertEqual(apply_plan(preview).find("Alice").account_id, 1)


class SwapTests(unittest.TestCase):
    def test_two_accounts_may_swap_non_reserved_primaries(self):
        accounts = [Account(1, "alice"), Account(2, "bob")]
        groups = [Group("swap", (1, 2), {1: "bob", 2: "alice"})]

        preview = plan(accounts, groups)

        self.assertEqual(preview.renames, {1: "bob", 2: "alice"})
        self.assertFalse(preview.is_deferred("swap"))
        state = apply_plan(preview)
        self.assertEqual(state.find("bob").account_id, 1)
        self.assertEqual(state.find("alice").account_id, 2)

    def test_swap_blocked_when_one_side_is_a_live_alias(self):
        # "bob" is still reserved by account 2's own live alias, so it is not
        # a swappable primary.
        accounts = [
            Account(1, "alice"),
            Account(2, "bob", (Alias("BOB", LATER),)),
        ]
        groups = [Group("swap", (1, 2), {1: "bob", 2: "alice"})]

        preview = plan(accounts, groups)

        self.assertEqual(preview.renames, {})
        self.assertTrue(preview.is_deferred("swap"))
        reasons = {c.reason for c in preview.conflicts}
        self.assertIn(RESERVED_BY_ALIAS, reasons)
        self.assertEqual(apply_plan(preview).find("bob").account_id, 2)

    def test_expired_alias_no_longer_blocks_a_swap(self):
        accounts = [
            Account(1, "alice"),
            Account(2, "bob", (Alias("BOB", EARLIER),)),
        ]
        groups = [Group("swap", (1, 2), {1: "bob", 2: "alice"})]

        preview = plan(accounts, groups)
        self.assertEqual(preview.renames, {1: "bob", 2: "alice"})

    def test_alias_expiring_on_migration_date_is_still_live(self):
        accounts = [
            Account(1, "alice"),
            Account(2, "bob", (Alias("BOB", ON),)),
        ]
        groups = [Group("swap", (1, 2), {1: "bob", 2: "alice"})]
        preview = plan(accounts, groups)
        self.assertEqual(preview.renames, {})
        self.assertTrue(preview.is_deferred("swap"))

    def test_swapping_a_reserved_primary_is_deferred(self):
        # "alice" is a non-reserved primary owned by 1, but account 2's own
        # live alias also reserves it; only non-reserved names may swap.
        accounts = [
            Account(1, "alice"),
            Account(2, "bob", (Alias("ALICE", EARLIER),)),
        ]
        groups = [Group("swap", (1, 2), {1: "bob", 2: "alice"})]
        preview = plan(accounts, groups)
        # Alias expired before the migration date, so the swap is allowed.
        self.assertEqual(preview.renames, {1: "bob", 2: "alice"})

        live = [
            Account(1, "alice"),
            Account(2, "bob", (Alias("ALICE", LATER),)),
        ]
        blocked = plan(live, [Group("swap", (1, 2), {1: "bob", 2: "alice"})])
        self.assertEqual(blocked.renames, {})
        self.assertTrue(blocked.is_deferred("swap"))
        self.assertIn(RESERVED_BY_ALIAS, {c.reason for c in blocked.conflicts})

    def test_swap_ignores_an_unrelated_third_party_alias(self):
        accounts = [
            Account(1, "alice"),
            Account(2, "bob"),
            Account(3, "carol", (Alias("dave", LATER),)),
        ]
        preview = plan(accounts, [Group("swap", (1, 2), {1: "bob", 2: "alice"})])
        self.assertEqual(preview.renames, {1: "bob", 2: "alice"})


class LiveAliasOccupancyTests(unittest.TestCase):
    def test_target_key_reserved_by_another_accounts_alias_is_deferred(self):
        accounts = [
            Account(1, "Alice", (Alias("Robert", LATER),)),
            Account(2, "Bob"),
        ]
        groups = [Group("g", (2,), {2: "robert"})]

        preview = plan(accounts, groups)

        self.assertEqual(preview.renames, {})
        conflict = [c for c in preview.conflicts if c.reason == RESERVED_BY_ALIAS][0]
        self.assertEqual(conflict.key, "robert")
        self.assertEqual(conflict.account_ids, (1, 2))

    def test_own_alias_spelling_can_be_absorbed_by_same_account(self):
        # One account, two spellings that the new rule folds together.
        accounts = [Account(1, "Straße", (Alias("STRASSE", LATER),))]
        groups = [Group("g", (1,), {1: "strasse"})]

        preview = plan(accounts, groups)

        self.assertEqual(preview.renames, {1: "strasse"})
        self.assertEqual(preview.conflicts, ())
        self.assertEqual(apply_plan(preview).find("STRASSE").account_id, 1)

    def test_expired_alias_frees_its_key(self):
        accounts = [
            Account(1, "Alice", (Alias("Robert", EARLIER),)),
            Account(2, "Bob"),
        ]
        groups = [Group("g", (2,), {2: "robert"})]

        preview = plan(accounts, groups)

        self.assertEqual(preview.renames, {2: "robert"})
        self.assertEqual(apply_plan(preview).find("robert").account_id, 2)


class ConnectedGroupTests(unittest.TestCase):
    def test_two_accounts_claiming_same_key_defers_both(self):
        accounts = [Account(1, "Alice"), Account(2, "Alicia")]
        groups = [
            Group("g1", (1,), {1: "shared"}),
            Group("g2", (2,), {2: "Shared"}),
        ]

        preview = plan(accounts, groups)

        self.assertEqual(preview.renames, {})
        self.assertTrue(preview.is_deferred("g1"))
        self.assertTrue(preview.is_deferred("g2"))
        duplicate = [
            c for c in preview.conflicts if c.reason == DUPLICATE_TARGET_KEY
        ]
        self.assertEqual(len(duplicate), 1)
        self.assertEqual(duplicate[0].account_ids, (1, 2))
        self.assertEqual(duplicate[0].group_names, ("g1", "g2"))

    def test_deferral_is_transitive_through_a_swap_chain(self):
        # g1 wants "bob", g2 swaps bob->carol with account 2, g3 wants "carol".
        accounts = [Account(1, "alice"), Account(2, "bob"), Account(3, "carol")]
        groups = [
            Group("g1", (1,), {1: "bob"}),
            Group("g2", (2,), {2: "carol"}),
            Group("g3", (3,), {3: "bob"}),
        ]

        preview = plan(accounts, groups)

        # g1 and g3 both claim bob; g2 (carol) is linked to g1/g3 through
        # bob as a source name.
        self.assertEqual(preview.renames, {})
        for name in ("g1", "g2", "g3"):
            self.assertTrue(preview.is_deferred(name))

    def test_independent_group_still_switches(self):
        accounts = [
            Account(1, "Alice"),
            Account(2, "Alicia"),
            Account(3, "Zoe"),
        ]
        groups = [
            Group("conflicting", (1,), {1: "shared"}),
            Group("also-conflicting", (2,), {2: "SHARED"}),
            Group("independent", (3,), {3: "zoe"}),
        ]

        preview = plan(accounts, groups)

        self.assertEqual(preview.renames, {3: "zoe"})
        self.assertFalse(preview.is_deferred("independent"))
        self.assertTrue(preview.is_deferred("conflicting"))

        state = apply_plan(preview)
        self.assertEqual(state.find("zoe").account_id, 3)
        # Deferred accounts keep their old primaries.
        self.assertEqual(state.find("Alice").account_id, 1)
        self.assertIsNone(state.find("shared"))

    def test_target_owned_by_ungrouped_account_is_deferred(self):
        accounts = [Account(1, "Target"), Account(2, "Alice")]
        groups = [Group("g", (2,), {2: "target"})]

        preview = plan(accounts, groups)

        self.assertEqual(preview.renames, {})
        reasons = {c.reason for c in preview.conflicts}
        self.assertIn(OWNED_BY_OTHER_ACCOUNT, reasons)


class PreviewAndApplyTests(unittest.TestCase):
    def test_preview_does_not_mutate_inputs(self):
        accounts = [Account(1, "Alice")]
        groups = [Group("g", (1,), {1: "alice"})]
        snapshot = (accounts[0].primary, groups[0].targets)

        preview = plan(accounts, groups)

        self.assertEqual(accounts[0].primary, snapshot[0])
        self.assertEqual(groups[0].targets, snapshot[1])
        self.assertEqual(preview.renames, {1: "alice"})

    def test_preview_lists_full_ownership_chain(self):
        accounts = [
            Account(1, "Alice"),
            Account(2, "Bob", (Alias("alice", LATER),)),
        ]
        groups = [Group("g", (1,), {1: "ALICE"})]

        preview = plan(accounts, groups)

        self.assertEqual(len(preview.conflicts), 1)
        conflict = preview.conflicts[0]
        self.assertEqual(conflict.reason, RESERVED_BY_ALIAS)
        self.assertEqual(conflict.key, "alice")
        self.assertEqual(conflict.account_ids, (1, 2))
        self.assertIn("g", conflict.group_names)

    def test_apply_twice_is_idempotent(self):
        accounts = [Account(1, "Alice"), Account(2, "Bob")]
        groups = [Group("g", (1,), {1: "alice"})]
        preview = plan(accounts, groups)

        first = apply_plan(preview)
        second = apply_plan(preview)

        self.assertEqual(first.account_ids(), second.account_ids())
        self.assertEqual(second.find("ALICE").account_id, 1)

    def test_plan_find_shortcut_matches_apply(self):
        accounts = [Account(1, "Alice")]
        groups = [Group("g", (1,), {1: "ALICE"})]
        preview = plan(accounts, groups)
        self.assertEqual(preview.find("alice").account_id, 1)

    def test_deferred_conflict_is_reported_together_with_all_causes(self):
        accounts = [
            Account(1, "alice", (Alias("reserved", LATER),)),
            Account(2, "bob"),
        ]
        groups = [Group("g", (2,), {2: "RESERVED"})]
        preview = plan(accounts, groups)

        self.assertEqual(len(preview.deferred), 1)
        deferred = preview.deferred[0]
        self.assertEqual(deferred.reason, COMPONENT_DEFERRED)
        self.assertIn(1, deferred.account_ids)
        self.assertIn(2, deferred.account_ids)
        self.assertEqual(preview.deferred_account_ids, (1, 2))

    def test_preexisting_primary_alias_collision_is_reported_and_ambiguous(self):
        # Bad data predating the migration: "alice" is both account 1's
        # primary and account 2's live alias.  The preview must surface the
        # ownership, and lookup must refuse to pick a winner.
        accounts = [
            Account(1, "alice"),
            Account(2, "bob", (Alias("ALICE", LATER),)),
        ]
        preview = plan(accounts, [])

        self.assertEqual(len(preview.conflicts), 1)
        conflict = preview.conflicts[0]
        self.assertEqual(conflict.reason, PREEXISTING_COLLISION)
        self.assertEqual(conflict.account_ids, (1, 2))
        self.assertEqual(conflict.key, "alice")
        with self.assertRaises(MigrationError):
            apply_plan(preview).find("alice")

    def test_three_way_rotation_applies_atomically(self):
        accounts = [Account(1, "a"), Account(2, "b"), Account(3, "c")]
        groups = [Group("cycle", (1, 2, 3), {1: "b", 2: "c", 3: "a"})]

        preview = plan(accounts, groups)

        self.assertEqual(preview.renames, {1: "b", 2: "c", 3: "a"})
        state = apply_plan(preview)
        self.assertEqual(state.find("a").account_id, 3)
        self.assertEqual(state.find("b").account_id, 1)
        self.assertEqual(state.find("c").account_id, 2)

    def test_group_members_without_targets_keep_their_names(self):
        preview = plan(
            [Account(1, "Alice"), Account(2, "Bob")], [Group("g", (1, 2))]
        )
        self.assertEqual(preview.renames, {})
        self.assertEqual(preview.conflicts, ())
        self.assertFalse(preview.is_deferred("g"))


class ValidationTests(unittest.TestCase):
    def test_legacy_collision_rejected(self):
        with self.assertRaises(MigrationError):
            plan([Account(1, "Alice"), Account(2, "alice")], [])

    def test_duplicate_account_id_rejected(self):
        with self.assertRaises(MigrationError):
            plan([Account(1, "Alice"), Account(1, "Bob")], [])

    def test_group_with_unknown_account_rejected(self):
        with self.assertRaises(MigrationError):
            plan([Account(1, "Alice")], [Group("g", (1, 99), {99: "x"})])

    def test_account_in_two_groups_rejected(self):
        with self.assertRaises(MigrationError):
            plan(
                [Account(1, "Alice")],
                [Group("g1", (1,)), Group("g2", (1,))],
            )

    def test_live_alias_collision_between_accounts_rejected(self):
        with self.assertRaises(MigrationError):
            plan(
                [
                    Account(1, "alice", (Alias("x", LATER),)),
                    Account(2, "bob", (Alias("X", LATER),)),
                ],
                [],
            )

    def test_conflict_object_is_plain_data(self):
        conflict = Conflict(
            reason=RESERVED_BY_ALIAS,
            key="k",
            account_ids=(1, 2),
            group_names=("g",),
        )
        self.assertIn("reserved-by-live-alias", str(conflict))
        self.assertEqual(conflict.account_ids, (1, 2))


if __name__ == "__main__":
    unittest.main()
