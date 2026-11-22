"""Stress / property-style tests over larger interleaved inputs.

These do not prove correctness by themselves, but they exercise the
statement-snapshot invariant and determinism across many generated shapes.
"""

from __future__ import annotations

import random
import unittest

from tests.support import (
    Rule,
    TargetTable,
    execute_merge,
    table,
)


def make_target(ids, rng):
    return TargetTable(
        (rid, {"k": f"k{rng.randrange(8)}", "v": rng.randrange(100)}) for rid in ids
    )


class SnapshotInvariantTests(unittest.TestCase):
    def test_every_binding_uses_statement_start_key(self):
        rng = random.Random(1234)
        for _ in range(50):
            target = make_target(range(6), rng)
            start = {row.row_id: ("k-" + row.values["k"], row.values["k"]) for row in target.rows}
            source = [{"k": "k" + str(rng.randrange(8)), "v": rng.randrange(100)} for _ in range(10)]
            rules = [Rule("update", assign={"k": lambda s, t: s["k"]}), Rule("insert")]
            outcome = execute_merge(source, target, ["k"], rules)
            # Each match that claims a row must have matched the snapshot key.
            for match in outcome.matches:
                if match.matched and match.target_row_id is not None:
                    row_original_key = dict(start)[match.target_row_id][1]
                    self.assertEqual(match.source_key, (row_original_key,))
                    self.assertEqual(match.target_key, (row_original_key,))
            # Every source row appears exactly once, in order.
            self.assertEqual([m.source_index for m in outcome.matches], list(range(len(source))))

    def test_duplicate_target_keys_are_flagged_not_silently_picked(self):
        target = table(
            ("t1", {"k": "x", "v": 1}),
            ("t2", {"k": "x", "v": 2}),
        )
        outcome = execute_merge([{"k": "x"}], target, ["k"], [Rule("delete")])
        self.assertFalse(outcome.committed)
        self.assertIn("duplicate_target_key", outcome.conflict_kinds())
        conflict = next(c for c in outcome.conflicts if c.kind == "duplicate_target_key")
        self.assertEqual(set(conflict.row_ids), {"t1", "t2"})
        self.assertEqual(conflict.key, ("x",))
        # Target untouched.
        self.assertEqual(len(outcome.target), 2)

    def test_duplicate_keys_unused_by_source_do_not_block_statement(self):
        target = table(
            ("t1", {"k": "x", "v": 1}),
            ("t2", {"k": "x", "v": 2}),
            ("t3", {"k": "y", "v": 3}),
        )
        outcome = execute_merge([{"k": "y"}], target, ["k"], [Rule("delete")])
        self.assertTrue(outcome.committed)
        self.assertNotIn(("t3",), [r.row_ids for r in outcome.conflicts])


class DeterminismTests(unittest.TestCase):
    def test_repeated_runs_are_identical(self):
        target = table(
            ("t1", {"k": "a", "v": 1}),
            ("t2", {"k": "b", "v": 2}),
        )
        source = [{"k": "a", "v": 9}, {"k": "c", "v": 3}, {"k": "b", "v": 8}]
        rules = [Rule("update", assign={"v": lambda s, t: s["v"]}), Rule("insert")]

        first = execute_merge(source, target, ["k"], rules)
        second = execute_merge(source, target, ["k"], rules)
        self.assertEqual(first.target.as_dict(), second.target.as_dict())
        self.assertEqual(
            [(c.source_index, c.action, c.target_row_id) for c in first.changes],
            [(c.source_index, c.action, c.target_row_id) for c in second.changes],
        )


class LargerInterleaveTests(unittest.TestCase):
    def test_mixed_batch_commits_with_expected_state(self):
        target = table(
            ("t1", {"k": "a", "v": 1}),
            ("t2", {"k": "b", "v": 1}),
            ("t3", {"k": "c", "v": 1}),
        )
        rules = [
            Rule("update", when="matched", predicate=lambda s, t: s["op"] == "u",
                 assign={"v": lambda s, t: t["v"] + 1}),
            Rule("delete", when="matched", predicate=lambda s, t: s["op"] == "d"),
            Rule("skip", when="matched"),
            Rule("insert", when="not_matched", predicate=lambda s, t: s["op"] != "x"),
            Rule("skip", when="not_matched"),
        ]
        source = [
            {"k": "a", "op": "u"},   # update t1 v=2
            {"k": "b", "op": "d"},   # delete t2
            {"k": "c", "op": "s"},   # skip
            {"k": "d", "op": "i"},   # insert
            {"k": "e", "op": "x"},   # not matched -> skip
        ]
        outcome = execute_merge(source, target, ["k"], rules)
        self.assertTrue(outcome.committed)
        rows = outcome.target.as_dict()
        self.assertEqual(rows["t1"]["v"], 2)
        self.assertNotIn("t2", rows)
        self.assertEqual(rows["t3"]["v"], 1)
        self.assertEqual(
            [(c.source_index, c.action) for c in outcome.changes],
            [(0, "update"), (1, "delete"), (3, "insert")],
        )
        self.assertEqual(
            [m.effective_action for m in outcome.matches],
            ["update", "delete", "skip", "insert", "skip"],
        )


if __name__ == "__main__":
    unittest.main()
