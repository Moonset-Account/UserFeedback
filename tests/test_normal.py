"""Normal-path tests: the happy path of MERGE plus mixed update/insert/delete."""

from __future__ import annotations

import unittest

from tests.support import (
    RelationStore,
    Rule,
    execute_merge,
    table,
)


class BasicUpdateInsertTests(unittest.TestCase):
    def test_update_matched_and_insert_unmatched(self):
        target = table(
            ("t1", {"id": 1, "sku": "a", "qty": 10}),
            ("t2", {"id": 2, "sku": "b", "qty": 20}),
        )
        rules = [
            Rule("update", assign={"qty": lambda s, t: t["qty"] + s["qty"]}),
            Rule("insert"),
        ]
        source = [
            {"sku": "a", "qty": 5},
            {"sku": "c", "qty": 7},
        ]
        outcome = execute_merge(source, target, ["sku"], rules)

        self.assertTrue(outcome.committed)
        self.assertEqual(outcome.conflicts, ())
        rows = outcome.target.as_dict()
        self.assertEqual(rows["t1"]["qty"], 15)
        self.assertEqual(rows["t2"]["qty"], 20)
        inserted = {rid: v for rid, v in rows.items() if rid not in ("t1", "t2")}
        self.assertEqual(len(inserted), 1)
        self.assertEqual(next(iter(inserted.values())), {"sku": "c", "qty": 7})

        actions = [c.action for c in outcome.changes]
        self.assertEqual(actions, ["update", "insert"])

    def test_update_records_before_and_after_values(self):
        target = table(("t1", {"sku": "a", "qty": 10, "note": "old"}))
        rules = [Rule("update", assign={"qty": 99, "note": "new"})]
        outcome = execute_merge([{"sku": "a", "qty": 1}], target, ["sku"], rules)

        self.assertTrue(outcome.committed)
        self.assertEqual(len(outcome.changes), 1)
        change = outcome.changes[0]
        self.assertEqual(change.target_row_id, "t1")
        self.assertEqual(change.before, {"sku": "a", "qty": 10, "note": "old"})
        self.assertEqual(change.after, {"sku": "a", "qty": 99, "note": "new"})
        self.assertEqual(change.key_before, ("a",))
        self.assertEqual(change.key_after, ("a",))

    def test_delete_matched_row(self):
        target = table(
            ("t1", {"sku": "a", "qty": 10}),
            ("t2", {"sku": "b", "qty": 20}),
        )
        rules = [Rule("delete")]
        outcome = execute_merge([{"sku": "a"}], target, ["sku"], rules)

        self.assertTrue(outcome.committed)
        self.assertEqual(outcome.target.row_ids, ("t2",))
        change = outcome.changes[0]
        self.assertEqual(change.action, "delete")
        self.assertEqual(change.after, None)
        self.assertEqual(change.before, {"sku": "a", "qty": 10})

    def test_mixed_update_delete_insert_in_source_order(self):
        target = table(
            ("t1", {"sku": "a", "qty": 1}),
            ("t2", {"sku": "b", "qty": 2}),
        )
        rules = [
            Rule("update", when="matched", predicate=lambda s, t: s["qty"] > 0,
                 assign={"qty": lambda s, t: t["qty"] + s["qty"]}),
            Rule("delete", when="matched"),
            Rule("insert"),
        ]
        source = [
            {"sku": "a", "qty": 10},   # update t1
            {"sku": "b", "qty": 0},    # falls through to delete t2
            {"sku": "c", "qty": 3},    # insert
        ]
        outcome = execute_merge(source, target, ["sku"], rules)

        self.assertTrue(outcome.committed)
        self.assertEqual([c.action for c in outcome.changes], ["update", "delete", "insert"])
        self.assertEqual([c.source_index for c in outcome.changes], [0, 1, 2])
        self.assertEqual([c.target_row_id for c in outcome.changes], ["t1", "t2", ("merge_insert", 2)])

    def test_insert_row_id_factory_is_deterministic_and_collision_safe(self):
        target = table(("t1", {"sku": "a", "qty": 1}))
        rules = [Rule("insert")]

        def factory(index, values):
            return "new-1"  # deliberately collides between the two inserts

        source = [{"sku": "b"}, {"sku": "c"}]
        first = execute_merge(source, target, ["sku"], rules, insert_row_id=factory)
        second = execute_merge(source, target, ["sku"], rules, insert_row_id=factory)

        self.assertTrue(first.committed)
        self.assertEqual(first.target.as_dict(), second.target.as_dict())
        self.assertEqual(len(first.target), 3)
        self.assertEqual(len(set(first.target.row_ids)), 3)


class RelationStoreTests(unittest.TestCase):
    def test_store_commits_on_success(self):
        store = RelationStore(table(("t1", {"sku": "a", "qty": 1})))
        outcome = store.merge(
            [{"sku": "a", "qty": 4}],
            ["sku"],
            [Rule("update", assign={"qty": lambda s, t: t["qty"] + s["qty"]})],
        )
        self.assertTrue(outcome.committed)
        self.assertEqual(store.table.as_dict()["t1"]["qty"], 5)
        self.assertEqual(len(store), 1)

    def test_store_is_untouched_on_config_error(self):
        store = RelationStore(table(("t1", {"sku": "a", "qty": 1})))
        with self.assertRaises(Exception):
            store.merge([{"sku": "a"}], ["sku"], [Rule("update")])  # missing assign
        self.assertEqual(store.table.as_dict(), {"t1": {"sku": "a", "qty": 1}})


if __name__ == "__main__":
    unittest.main()
