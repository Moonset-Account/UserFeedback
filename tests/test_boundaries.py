"""Boundary tests: NULL keys, empty inputs, composite keys, rule validation."""

from __future__ import annotations

import unittest

from tests.support import (
    MergeConfigError,
    Rule,
    TargetTable,
    execute_merge,
    table,
)


class NullSemanticsTests(unittest.TestCase):
    def test_null_source_key_never_matches_null_target(self):
        target = table(("t1", {"sku": None, "qty": 1}))
        rules = [Rule("update", assign={"qty": 9}), Rule("insert")]
        outcome = execute_merge([{"sku": None, "qty": 5}], target, ["sku"], rules)

        self.assertTrue(outcome.committed)
        match = outcome.matches[0]
        self.assertFalse(match.matched)
        self.assertEqual(match.effective_action, "insert")
        self.assertEqual(match.reason, "null_key_never_matches")
        # original NULL-key row untouched, one insert appended
        self.assertEqual(len(outcome.target), 2)
        self.assertEqual(outcome.target.as_dict()["t1"], {"sku": None, "qty": 1})

    def test_null_in_composite_key_disables_match(self):
        target = table(("t1", {"a": 1, "b": None, "v": 0}))
        rules = [Rule("update", assign={"v": 100}), Rule("insert")]
        outcome = execute_merge([{"a": 1, "b": None, "v": 1}], target, ["a", "b"], rules)

        self.assertTrue(outcome.committed)
        self.assertFalse(outcome.matches[0].matched)
        self.assertEqual(outcome.target.as_dict()["t1"]["v"], 0)

    def test_missing_column_is_treated_as_null(self):
        target = table(("t1", {"sku": None, "qty": 1}))
        rules = [Rule("insert")]
        # source omits 'sku' entirely -> key (None,) -> never matches
        outcome = execute_merge([{"qty": 2}], target, ["sku"], rules)
        self.assertTrue(outcome.committed)
        self.assertFalse(outcome.matches[0].matched)

    def test_null_key_update_targeting_null_row_stays_unmatched(self):
        # A matched-rule update can never fire for a NULL-key source row.
        target = table(("t1", {"sku": None, "qty": 1}))
        outcome = execute_merge([{"sku": None, "qty": 3}], target, ["sku"], [Rule("update", assign={"qty": 0})])
        self.assertTrue(outcome.committed)
        self.assertEqual(outcome.changes, ())
        self.assertEqual(outcome.target.as_dict()["t1"]["qty"], 1)
        self.assertEqual(outcome.matches[0].effective_action, "none")


class EmptyInputTests(unittest.TestCase):
    def test_empty_source_is_a_no_op(self):
        target = table(("t1", {"sku": "a", "qty": 1}))
        outcome = execute_merge([], target, ["sku"], [Rule("update", assign={"qty": 0})])
        self.assertTrue(outcome.committed)
        self.assertEqual(outcome.changes, ())
        self.assertEqual(outcome.matches, ())
        self.assertEqual(outcome.target.as_dict(), {"t1": {"sku": "a", "qty": 1}})

    def test_empty_target_inserts_all(self):
        target = TargetTable(())
        rules = [Rule("insert")]
        outcome = execute_merge([{"sku": "a"}, {"sku": "b"}], target, ["sku"], rules)
        self.assertTrue(outcome.committed)
        self.assertEqual(len(outcome.target), 2)
        self.assertEqual([c.action for c in outcome.changes], ["insert", "insert"])


class CompositeKeyTests(unittest.TestCase):
    def test_composite_key_matches_only_on_full_key(self):
        target = table(
            ("t1", {"region": "eu", "sku": "a", "qty": 1}),
            ("t2", {"region": "us", "sku": "a", "qty": 2}),
        )
        rules = [Rule("update", assign={"qty": lambda s, t: t["qty"] + s["qty"]}), Rule("insert")]
        source = [
            {"region": "eu", "sku": "a", "qty": 10},
            {"region": "us", "sku": "a", "qty": 20},
        ]
        outcome = execute_merge(source, target, ["region", "sku"], rules)
        self.assertTrue(outcome.committed)
        rows = outcome.target.as_dict()
        self.assertEqual(rows["t1"]["qty"], 11)
        self.assertEqual(rows["t2"]["qty"], 22)

    def test_partial_key_does_not_match(self):
        target = table(("t1", {"region": "eu", "sku": "a", "qty": 1}))
        rules = [Rule("update", assign={"qty": 0}), Rule("insert")]
        outcome = execute_merge([{"region": "eu", "qty": 5}], target, ["region", "sku"], rules)
        self.assertTrue(outcome.committed)
        self.assertFalse(outcome.matches[0].matched)
        self.assertEqual(outcome.target.as_dict()["t1"]["qty"], 1)


class RuleValidationTests(unittest.TestCase):
    def test_update_without_assign_is_rejected(self):
        with self.assertRaises(MergeConfigError):
            Rule("update")

    def test_delete_with_assign_is_rejected(self):
        with self.assertRaises(MergeConfigError):
            Rule("delete", assign={"x": 1})

    def test_insert_on_matched_is_rejected(self):
        with self.assertRaises(MergeConfigError):
            Rule("insert", when="matched")

    def test_update_on_not_matched_is_rejected(self):
        with self.assertRaises(MergeConfigError):
            Rule("update", when="not_matched", assign={"x": 1})

    def test_skip_requires_explicit_side(self):
        with self.assertRaises(MergeConfigError):
            Rule("skip")

    def test_unknown_action_and_when(self):
        with self.assertRaises(MergeConfigError):
            Rule("upsert", assign={"x": 1})
        with self.assertRaises(MergeConfigError):
            Rule("skip", when="maybe")

    def test_empty_match_columns_rejected(self):
        with self.assertRaises(MergeConfigError):
            execute_merge([{"a": 1}], TargetTable(()), [], [Rule("insert")])

    def test_duplicate_row_ids_rejected(self):
        with self.assertRaises(MergeConfigError):
            TargetTable([("t1", {"a": 1}), ("t1", {"a": 2})])

    def test_none_row_id_rejected(self):
        with self.assertRaises(MergeConfigError):
            TargetTable([(None, {"a": 1})])

    def test_bad_source_row_type_rejected(self):
        with self.assertRaises(MergeConfigError):
            execute_merge([("sku", "a")], TargetTable(()), ["sku"], [Rule("insert")])

    def test_rule_when_aliases_normalized(self):
        rule = Rule("insert", when="not matched")
        self.assertEqual(rule.when, "not_matched")

    def test_unhashable_match_value_is_config_error(self):
        with self.assertRaises(MergeConfigError):
            execute_merge(
                [{"k": ["a"]}],
                TargetTable(()),
                ["k"],
                [Rule("insert")],
            )

    def test_unhashable_target_match_value_is_config_error(self):
        with self.assertRaises(MergeConfigError):
            execute_merge(
                [{"k": "a"}],
                TargetTable([("t1", {"k": ["a"]})]),
                ["k"],
                [Rule("insert")],
            )


class NoRuleTests(unittest.TestCase):
    def test_source_row_without_applicable_rule_is_left_alone(self):
        target = table(("t1", {"sku": "a", "qty": 1}))
        # only a not-matched rule exists -> matched source row does nothing
        outcome = execute_merge([{"sku": "a", "qty": 9}], target, ["sku"], [Rule("insert")])
        self.assertTrue(outcome.committed)
        self.assertEqual(outcome.matches[0].effective_action, "none")
        self.assertEqual(outcome.changes, ())
        self.assertEqual(outcome.target.as_dict()["t1"]["qty"], 1)

    def test_skip_rule_discards_matched_row_without_insert(self):
        target = table(("t1", {"sku": "a", "qty": 1}))
        rules = [Rule("skip", when="matched"), Rule("insert")]
        outcome = execute_merge([{"sku": "a", "qty": 9}], target, ["sku"], rules)
        self.assertTrue(outcome.committed)
        self.assertEqual(outcome.target.as_dict(), {"t1": {"sku": "a", "qty": 1}})
        self.assertEqual(outcome.changes, ())
        self.assertEqual(outcome.matches[0].effective_action, "skip")


if __name__ == "__main__":
    unittest.main()
