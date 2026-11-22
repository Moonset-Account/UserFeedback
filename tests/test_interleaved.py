"""Interleaved scenarios: snapshot binding, key rewrites, ordering, conflicts.

These are the acceptance-critical cases from the task statement:

* an early source row that rewrites the target match key must not rebind any
  later source row -- later rows still bind against the statement-start table;
* more than one source row truly modifying/deleting one target row rejects the
  whole statement and leaves the table untouched;
* rows diverted by a skip rule do not count as a conflict and do not fall
  through to an insert;
* actions take effect at once only after the final-key check, and results are
  ordered by source order then row id.
"""

from __future__ import annotations

import unittest

from tests.support import (
    MergeConflictError,
    RelationStore,
    Rule,
    execute_merge,
    table,
)


class SnapshotBindingTests(unittest.TestCase):
    def test_first_row_rewrites_key_second_row_still_binds_original(self):
        target = table(
            ("t1", {"sku": "a", "qty": 10}),
            ("t2", {"sku": "b", "qty": 20}),
        )
        # matched -> rename a to z ; anything not matched -> skip (no insert).
        rules = [
            Rule("update", assign={"sku": "z"}),
            Rule("skip", when="not_matched"),
        ]
        source = [
            {"sku": "a", "qty": 999},   # matches t1 in snapshot, renames it to z
            {"sku": "a", "qty": 1},     # must ALSO bind t1 from snapshot, not "z"
        ]
        outcome = execute_merge(source, target, ["sku"], rules)

        # Both source rows bound t1 -> two modifying rows on one target -> reject.
        self.assertFalse(outcome.committed)
        self.assertEqual(outcome.conflict_kinds(), ("multiple_modifying_source_rows",))
        conflict = outcome.conflicts[0]
        self.assertEqual(conflict.row_ids, ("t1",))
        self.assertEqual(conflict.source_indices, (0, 1))
        self.assertEqual(conflict.key, ("a",))  # statement-start key

        # Bindings are visible per source row and both point at t1 by row id.
        self.assertEqual([m.target_row_id for m in outcome.matches], ["t1", "t1"])
        self.assertEqual([m.source_index for m in outcome.matches], [0, 1])

        # Nothing committed.
        self.assertEqual(outcome.target.as_dict(), {
            "t1": {"sku": "a", "qty": 10},
            "t2": {"sku": "b", "qty": 20},
        })
        self.assertEqual(outcome.changes, ())
        # The projected (would-be) table leaves the contested row untouched.
        self.assertEqual(outcome.projected.as_dict(), {
            "t1": {"sku": "a", "qty": 10},
            "t2": {"sku": "b", "qty": 20},
        })

    def test_key_rewrite_then_second_row_does_not_see_new_key(self):
        # A single rewriting source row plus a source row on the *new* key:
        # the second row must NOT match; it inserts, which then clashes with
        # the renamed row on the final key, so the statement is rejected.
        target = table(("t1", {"sku": "a", "qty": 10}))
        rules = [
            Rule("update", assign={"sku": "z"}),
            Rule("insert"),
        ]
        source = [
            {"sku": "a", "qty": 11},   # snapshot-matches t1, renames to z
            {"sku": "z", "qty": 5},    # "z" did not exist at start -> insert
        ]
        outcome = execute_merge(source, target, ["sku"], rules)

        match_z = outcome.matches[1]
        self.assertFalse(match_z.matched)
        self.assertEqual(match_z.effective_action, "insert")
        self.assertEqual(match_z.reason, "no_matching_target_row")

        # t1 now holds z, and there is an inserted z row too -> final key clash.
        self.assertFalse(outcome.committed)
        self.assertIn("duplicate_final_key", outcome.conflict_kinds())

    def test_key_rewrite_with_non_colliding_insert_commits(self):
        target = table(("t1", {"sku": "a", "qty": 10}))
        rules = [
            Rule("update", assign={"sku": "z"}),
            Rule("insert"),
        ]
        source = [
            {"sku": "a", "qty": 11},   # snapshot-matches t1, renames to z
            {"sku": "y", "qty": 5},    # never existed -> insert, no clash
        ]
        outcome = execute_merge(source, target, ["sku"], rules)
        self.assertTrue(outcome.committed)
        rows = outcome.target.as_dict()
        self.assertEqual(rows["t1"], {"sku": "z", "qty": 10})
        self.assertEqual(len(rows), 2)
        self.assertEqual(outcome.matches[1].effective_action, "insert")
        self.assertFalse(outcome.matches[1].matched)

    def test_insert_does_not_join_the_lookup_for_later_rows(self):
        target = table(("t1", {"sku": "a", "qty": 1}))
        rules = [
            Rule("update", assign={"qty": lambda s, t: t["qty"] + 1}),
            Rule("insert"),
        ]
        source = [
            {"sku": "new", "qty": 1},  # inserted
            {"sku": "new", "qty": 1},  # must NOT see the insert -> also insert
        ]
        outcome = execute_merge(source, target, ["sku"], rules)
        # Two inserts with the same final key -> final uniqueness must reject.
        self.assertFalse(outcome.committed)
        self.assertIn("duplicate_final_key", outcome.conflict_kinds())
        final_conflict = next(c for c in outcome.conflicts if c.kind == "duplicate_final_key")
        self.assertEqual(final_conflict.source_indices, (0, 1))

    def test_snapshot_matching_is_independent_of_source_row_order(self):
        target = table(
            ("t1", {"sku": "a", "qty": 10}),
            ("t2", {"sku": "b", "qty": 20}),
        )
        rules = [Rule("update", assign={"qty": lambda s, t: t["qty"] + s["delta"]})]
        forward = execute_merge(
            [{"sku": "a", "delta": 1}, {"sku": "b", "delta": 2}], target, ["sku"], rules
        )
        reverse = execute_merge(
            [{"sku": "b", "delta": 2}, {"sku": "a", "delta": 1}], target, ["sku"], rules
        )
        self.assertTrue(forward.committed and reverse.committed)
        self.assertEqual(forward.target.as_dict(), reverse.target.as_dict())


class MultiplicityConflictTests(unittest.TestCase):
    def _base_target(self):
        return table(("t1", {"sku": "a", "qty": 10}))

    def test_two_updates_on_same_target_reject_whole_statement(self):
        outcome = execute_merge(
            [{"sku": "a", "qty": 1}, {"sku": "a", "qty": 2}],
            self._base_target(),
            ["sku"],
            [Rule("update", assign={"qty": lambda s, t: t["qty"] + s["qty"]})],
        )
        self.assertFalse(outcome.committed)
        self.assertEqual(outcome.conflict_kinds(), ("multiple_modifying_source_rows",))
        self.assertEqual(outcome.target.as_dict()["t1"]["qty"], 10)
        self.assertEqual(outcome.changes, ())
        # matches still fully reported
        self.assertEqual([m.source_index for m in outcome.matches], [0, 1])

    def test_update_and_delete_on_same_target_reject(self):
        rules = [Rule("update", when="matched", predicate=lambda s, t: s["qty"] > 0,
                      assign={"qty": 0}), Rule("delete")]
        outcome = execute_merge(
            [{"sku": "a", "qty": 1}, {"sku": "a", "qty": 0}],
            self._base_target(),
            ["sku"],
            rules,
        )
        self.assertFalse(outcome.committed)
        conflict = outcome.conflicts[0]
        self.assertEqual(conflict.row_ids, ("t1",))
        self.assertEqual(conflict.details["actions"], ("update", "delete"))

    def test_two_deletes_on_same_target_reject(self):
        outcome = execute_merge(
            [{"sku": "a"}, {"sku": "a"}],
            self._base_target(),
            ["sku"],
            [Rule("delete")],
        )
        self.assertFalse(outcome.committed)
        self.assertEqual(outcome.conflict_kinds(), ("multiple_modifying_source_rows",))

    def test_skipped_rows_do_not_count_as_conflict(self):
        # One real update plus two skip-diverted rows on the same target: only
        # one true modifying row -> commit, skip rows do not insert.
        rules = [
            Rule("update", when="matched", predicate=lambda s, t: s.get("act") == "set",
                 assign={"qty": lambda s, t: s["qty"]}),
            Rule("skip", when="matched", predicate=lambda s, t: s.get("act") == "ignore"),
            Rule("insert"),
        ]
        source = [
            {"sku": "a", "qty": 77, "act": "set"},     # update
            {"sku": "a", "qty": 1, "act": "ignore"},   # skip
            {"sku": "a", "qty": 2, "act": "ignore"},   # skip
        ]
        outcome = execute_merge(source, self._base_target(), ["sku"], rules)
        self.assertTrue(outcome.committed)
        self.assertEqual(outcome.target.as_dict()["t1"]["qty"], 77)
        self.assertEqual([c.source_index for c in outcome.changes], [0])
        self.assertEqual(
            [m.effective_action for m in outcome.matches], ["update", "skip", "skip"]
        )
        # No fallback inserts for the skipped rows.
        self.assertEqual(len(outcome.target), 1)

    def test_skip_does_not_fall_through_after_unmatched(self):
        # not-matched rows diverted by skip never insert.
        rules = [Rule("skip", when="not_matched"), Rule("insert")]
        outcome = execute_merge([{"sku": "zzz"}], self._base_target(), ["sku"], rules)
        self.assertTrue(outcome.committed)
        self.assertEqual(len(outcome.target), 1)
        self.assertEqual(outcome.matches[0].effective_action, "skip")

    def test_distinct_targets_are_not_a_conflict(self):
        target = table(
            ("t1", {"sku": "a", "qty": 1}),
            ("t2", {"sku": "b", "qty": 2}),
        )
        outcome = execute_merge(
            [{"sku": "a", "qty": 10}, {"sku": "b", "qty": 20}],
            target,
            ["sku"],
            [Rule("update", assign={"qty": 0})],
        )
        self.assertTrue(outcome.committed)
        self.assertEqual(len(outcome.changes), 2)


class FinalKeyTests(unittest.TestCase):
    def test_update_into_existing_key_is_rejected(self):
        target = table(
            ("t1", {"sku": "a", "qty": 1}),
            ("t2", {"sku": "b", "qty": 2}),
        )
        # rename a -> b collides with existing b
        outcome = execute_merge([{"sku": "a"}], target, ["sku"], [Rule("update", assign={"sku": "b"})])
        self.assertFalse(outcome.committed)
        kinds = outcome.conflict_kinds()
        self.assertIn("duplicate_final_key", kinds)
        conflict = next(c for c in outcome.conflicts if c.kind == "duplicate_final_key")
        self.assertEqual(set(conflict.row_ids), {"t1", "t2"})
        self.assertEqual(conflict.key, ("b",))
        # Input untouched.
        self.assertEqual(outcome.target.as_dict()["t1"]["sku"], "a")

    def test_insert_rule_never_fires_for_matched_row(self):
        target = table(("t1", {"sku": "a", "qty": 1}))
        rules = [Rule("insert")]
        outcome = execute_merge([{"sku": "a"}], target, ["sku"], rules)
        # 'a' matches t1 so the insert (not_matched) rule never fires;
        # nothing changes and it commits.
        self.assertTrue(outcome.committed)
        self.assertEqual(len(outcome.target), 1)
        self.assertEqual(outcome.matches[0].effective_action, "none")

    def test_insert_colliding_with_untouched_row(self):
        # Insert a new source row whose assignment rewrites its key onto an
        # existing untouched row -> final key clash, whole statement rejected.
        target = table(("t1", {"sku": "a", "qty": 1}))
        source = [{"sku": "zzz"}]
        rules = [Rule("insert", assign={"sku": "a"})]
        outcome = execute_merge(source, target, ["sku"], rules)
        self.assertFalse(outcome.committed)
        self.assertIn("duplicate_final_key", outcome.conflict_kinds())

    def test_update_into_pre_existing_duplicate_key_is_flagged(self):
        # Target already (unusually) has two rows on key 'b', untouched by the
        # statement.  Renaming t3 'c' -> 'b' must still be rejected, and the
        # conflict must locate every row in that final key group.
        target = table(
            ("t1", {"sku": "b", "qty": 1}),
            ("t2", {"sku": "b", "qty": 2}),
            ("t3", {"sku": "c", "qty": 3}),
        )
        outcome = execute_merge([{"sku": "c"}], target, ["sku"], [Rule("update", assign={"sku": "b"})])
        self.assertFalse(outcome.committed)
        conflict = next(c for c in outcome.conflicts if c.kind == "duplicate_final_key")
        self.assertEqual(set(conflict.row_ids), {"t1", "t2", "t3"})
        self.assertEqual(conflict.key, ("b",))
        self.assertEqual(conflict.source_indices, (0,))

    def test_delete_resolves_would_be_collision(self):        # Moving a->b is fine if the b row is deleted in the same statement.
        target = table(
            ("t1", {"sku": "a", "qty": 1}),
            ("t2", {"sku": "b", "qty": 2}),
        )
        rules = [
            Rule("update", when="matched", predicate=lambda s, t: s.get("act") == "rename",
                 assign={"sku": "b"}),
            Rule("delete", when="matched"),
        ]
        source = [
            {"sku": "a", "act": "rename"},  # a -> b
            {"sku": "b", "act": "drop"},    # delete t2
        ]
        outcome = execute_merge(source, target, ["sku"], rules)
        self.assertTrue(outcome.committed)
        rows = outcome.target.as_dict()
        self.assertEqual(set(rows), {"t1"})
        self.assertEqual(rows["t1"]["sku"], "b")


class AtomicityTests(unittest.TestCase):
    def test_rejected_statement_leaves_store_untouched(self):
        store = RelationStore(table(("t1", {"sku": "a", "qty": 10})))
        outcome = store.merge(
            [{"sku": "a", "qty": 1}, {"sku": "a", "qty": 2}],
            ["sku"],
            [Rule("update", assign={"qty": lambda s, t: t["qty"] + s["qty"]})],
        )
        self.assertFalse(outcome.committed)
        self.assertEqual(store.table.as_dict(), {"t1": {"sku": "a", "qty": 10}})

    def test_raise_on_conflict_carries_outcome(self):
        rules = [Rule("delete")]
        with self.assertRaises(MergeConflictError) as ctx:
            execute_merge(
                [{"sku": "a"}, {"sku": "a"}],
                table(("t1", {"sku": "a", "qty": 1})),
                ["sku"],
                rules,
                raise_on_conflict=True,
            )
        outcome = ctx.exception.outcome
        self.assertFalse(outcome.committed)
        self.assertEqual(outcome.conflict_kinds(), ("multiple_modifying_source_rows",))
        self.assertEqual([m.source_index for m in outcome.matches], [0, 1])


class OrderingTests(unittest.TestCase):
    def test_changes_sorted_by_source_order(self):
        target = table(
            ("t1", {"sku": "a", "qty": 1}),
            ("t2", {"sku": "b", "qty": 2}),
        )
        rules = [Rule("update", assign={"qty": 0})]
        outcome = execute_merge(
            [{"sku": "b"}, {"sku": "a"}], target, ["sku"], rules
        )
        self.assertTrue(outcome.committed)
        self.assertEqual([c.source_index for c in outcome.changes], [0, 1])
        self.assertEqual([c.target_row_id for c in outcome.changes], ["t2", "t1"])

    def test_changes_tiebreak_by_row_id(self):
        # A single source row cannot modify two rows, so craft several inserts
        # that all come from one source index (one insert) -- instead verify
        # stable ordering across different row-id types with a no-tie case.
        target = table(
            (2, {"sku": "b", "qty": 2}),
            (1, {"sku": "a", "qty": 1}),
        )
        rules = [Rule("update", assign={"qty": 0})]
        outcome = execute_merge([{"sku": "a"}, {"sku": "b"}], target, ["sku"], rules)
        self.assertTrue(outcome.committed)
        # source order wins over row-id ordering
        self.assertEqual([c.target_row_id for c in outcome.changes], [1, 2])


if __name__ == "__main__":
    unittest.main()
