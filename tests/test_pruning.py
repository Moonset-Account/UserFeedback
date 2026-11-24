from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pruning import (  # noqa: E402
    ColumnStats,
    Decision,
    col,
    is_null,
    lit,
    parse_expr,
    prune,
    prune_from_parts,
)
from pruning.expressions import and_, not_, or_  # noqa: E402


def stats(**columns):
    return columns


class IntervalPruningTests(unittest.TestCase):
    def test_two_individually_possible_conditions_are_jointly_impossible(self):
        column_stats = stats(x=ColumnStats(0, 100, False))

        first = prune("x > 60", column_stats)
        second = prune("x < 40", column_stats)
        joint = prune("x > 60 AND x < 40", column_stats)

        self.assertTrue(first.needs_read)
        self.assertTrue(second.needs_read)
        self.assertEqual(joint.decision, Decision.SKIP)
        self.assertTrue(joint.skipped)
        text = "\n".join(joint.reasons)
        self.assertIn("x >= 61", text)
        self.assertIn("x <= 39", text)
        self.assertIn("-22", text)

    def test_inclusive_integer_boundaries(self):
        column_stats = stats(x=ColumnStats(1, 5, False))

        self.assertTrue(prune("x >= 1 AND x <= 5", column_stats).all_passed)
        self.assertTrue(prune("x > 0 AND x < 6", column_stats).all_passed)
        self.assertEqual(
            prune("x > 1 AND x < 2", column_stats).decision, Decision.SKIP
        )
        self.assertEqual(
            prune("x >= 2 AND x <= 1", column_stats).decision, Decision.SKIP
        )

    def test_or_branches_and_tautology(self):
        column_stats = stats(x=ColumnStats(0, 10, False))

        impossible = prune("x < 0 OR x > 10", column_stats)
        self.assertEqual(impossible.decision, Decision.SKIP)
        self.assertEqual(
            prune("x < 1 OR x > 9", column_stats).decision, Decision.READ
        )
        tautology = prune("x >= 0 OR x < 0", column_stats)
        self.assertTrue(tautology.all_passed)

    def test_not_uses_three_valued_complement(self):
        column_stats = stats(x=ColumnStats(0, 10, False))
        self.assertTrue(prune("NOT (x < 0)", column_stats).all_passed)
        self.assertTrue(prune("NOT (x > 10)", column_stats).all_passed)
        self.assertTrue(prune("NOT (x > 5)", column_stats).needs_read)

        # The inner conjunction has no TRUE row, so its negation is TRUE for
        # every non-NULL row.
        self.assertTrue(
            prune("NOT (x > 50 AND x < 40)", column_stats).all_passed
        )

        nullable = stats(x=ColumnStats(0, 10, True))
        # NULL makes NOT(x > 5) UNKNOWN, so the group cannot fully pass.
        self.assertTrue(prune("NOT (x > 5)", nullable).needs_read)
        # NULL also makes the negated impossible conjunction UNKNOWN.
        negated = prune("NOT (x > 50 AND x < 40)", nullable)
        self.assertTrue(negated.needs_read)
        self.assertIsNone(negated.not_true_witness["x"])


class NullSemanticsTests(unittest.TestCase):
    def test_all_null_column(self):
        column_stats = stats(x=ColumnStats(None, None, True, True))

        comparison = prune("x > 1", column_stats)
        null_check = prune("x IS NULL", column_stats)
        not_null_check = prune("x IS NOT NULL", column_stats)

        self.assertTrue(comparison.skipped)
        self.assertTrue(null_check.all_passed)
        self.assertTrue(not_null_check.skipped)

    def test_non_null_column(self):
        column_stats = stats(x=ColumnStats(0, 10, False))
        self.assertTrue(prune("x IS NULL", column_stats).skipped)
        self.assertTrue(prune("x IS NOT NULL", column_stats).all_passed)

    def test_possible_null_blocks_comparison_pass_but_null_branch_can_restore_it(self):
        column_stats = stats(x=ColumnStats(2, 10, True, False))

        # Every non-NULL value passes, but a NULL row is UNKNOWN.
        comparison = prune("x >= 2", column_stats)
        self.assertTrue(comparison.needs_read)
        self.assertIsNotNone(comparison.not_true_witness)
        self.assertIsNone(comparison.not_true_witness["x"])

        # SQL OR with TRUE from IS NULL makes every row TRUE.
        restored = prune("x >= 2 OR x IS NULL", column_stats)
        self.assertTrue(restored.all_passed)

    def test_comparison_to_sql_null_is_always_unknown(self):
        column_stats = stats(x=ColumnStats(0, 10, False))
        result = prune("x = NULL", column_stats)
        self.assertTrue(result.skipped)  # Never TRUE.
        self.assertTrue(prune("(x = NULL) IS NULL", column_stats).all_passed)


class RelationshipTests(unittest.TestCase):
    def test_confirmed_relation_proves_whole_group_pass(self):
        column_stats = stats(
            x=ColumnStats(0, 100, False), y=ColumnStats(0, 100, False)
        )
        result = prune("x < y", column_stats, relationships=["x < y"])
        self.assertTrue(result.all_passed)
        self.assertIn("y - x <= 0", result.not_true_paths[2].reason or "")

    def test_relation_combines_with_filter_constraints_to_skip(self):
        column_stats = stats(
            x=ColumnStats(0, 100, False), y=ColumnStats(0, 100, False)
        )
        result = prune(
            "x > 50 AND y < 40",
            column_stats,
            relationships=[("x", "<", "y")],
        )
        self.assertTrue(result.skipped)
        text = "\n".join(result.reasons)
        self.assertIn("x - y <= -1", text)
        self.assertIn("y <= 39", text)
        self.assertIn("x >= 51", text)

    def test_null_endpoint_escapes_row_relation_for_existence_witness(self):
        column_stats = stats(
            x=ColumnStats(0, 100, True, False),
            y=ColumnStats(0, 100, True, False),
        )
        # Even with x < y confirmed for non-NULL pairs, x IS NULL is possible.
        result = prune("x IS NULL", column_stats, relationships=["x < y"])
        self.assertTrue(result.needs_read)
        self.assertIsNone(result.true_witness["x"])


class ApiAndReasonTests(unittest.TestCase):
    def test_accepts_parser_ast_and_tuple_dsl(self):
        column_stats = stats(x=ColumnStats(0, 100, False))
        expression = "x > 10 AND x < 20"
        ast = parse_expr(expression)
        tuple_dsl = ("and", ("gt", "x", 10), ("lt", "x", 20))
        builder = and_(col("x").gt(lit(10)), col("x").lt(20))

        for candidate in (expression, ast, tuple_dsl, builder):
            with self.subTest(candidate=candidate):
                result = prune(candidate, column_stats)
                self.assertTrue(result.needs_read)
                self.assertTrue(result.reasons)

    def test_result_dictionary_and_status_aliases(self):
        result = prune("FALSE", {})
        payload = result.to_dict()
        self.assertTrue(result.skipped)
        self.assertEqual(result.status, "SKIP")
        self.assertEqual(payload["decision"], "skip")
        self.assertTrue(payload["skipped"])

    def test_missing_stats_are_conservative_but_tautology_still_passes(self):
        result = prune("x IS NULL OR x IS NOT NULL", {})
        self.assertTrue(result.all_passed)
        unknown = prune("x > 3", {})
        self.assertTrue(unknown.needs_read)

    def test_raw_parts_and_relation_mapping_apis(self):
        result = prune_from_parts(
            "x < y",
            bounds={"x": (0, 9), "y": (10, 20)},
            may_be_null={"x": False, "y": False},
            all_null={"x": False, "y": False},
            relationships={"x": {"<": "y"}},
        )
        self.assertTrue(result.all_passed)

        tuple_result = prune(
            "x > 100 AND x < 0",
            {"x": (0, 50, False, False)},
        )
        self.assertTrue(tuple_result.skipped)

        single_relation = prune(
            "x < y",
            {
                "x": ColumnStats(0, 9, False),
                "y": ColumnStats(10, 20, False),
            },
            ("x", "<", "y"),
        )
        self.assertTrue(single_relation.all_passed)

    def test_read_witnesses_are_present(self):
        result = prune(
            "x > 5 OR x < 1",
            stats(x=ColumnStats(0, 10, False)),
        )
        self.assertTrue(result.needs_read)
        self.assertIsNotNone(result.true_witness)
        self.assertIsNotNone(result.not_true_witness)
        self.assertGreater(result.true_witness["x"], 5)
        self.assertBetween(result.not_true_witness["x"], 1, 5)

    def assertBetween(self, value, lower, upper):
        self.assertIsNotNone(value)
        self.assertGreaterEqual(value, lower)
        self.assertLessEqual(value, upper)


if __name__ == "__main__":
    unittest.main()
