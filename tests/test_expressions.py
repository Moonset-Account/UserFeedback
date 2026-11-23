"""Unit tests for 3VL evaluation and the symbolic null-rejection proof."""

import unittest

from predrewrite.expressions import (
    And,
    Column,
    Compare,
    IsNull,
    Literal,
    Not,
    Or,
    and_exprs,
    eval_expr,
    expr_to_text,
    is_null_rejecting,
    null_status_explanation,
    referenced_aliases,
    split_conjuncts,
)


def col(alias: str, name: str) -> Column:
    return Column(alias, name)


class ThreeValuedLogicTests(unittest.TestCase):
    def setUp(self) -> None:
        # One row where b.x is NULL, b.y = 2, a.x = 1.
        self.row = {("a", "x"): 1, ("b", "x"): None, ("b", "y"): 2}

    def test_comparison_with_null_is_unknown(self) -> None:
        self.assertIsNone(eval_expr(Compare(col("b", "x"), "=", Literal(1)), self.row))
        self.assertIsNone(eval_expr(Compare(col("b", "x"), "<>", Literal(1)), self.row))
        self.assertIsNone(eval_expr(Compare(col("b", "x"), ">", Literal(1)), self.row))

    def test_null_literal_comparison_is_unknown(self) -> None:
        expr = Compare(col("a", "x"), "=", Literal(None))
        self.assertIsNone(eval_expr(expr, self.row))

    def test_is_null_and_is_not_null(self) -> None:
        self.assertIs(eval_expr(IsNull(col("b", "x")), self.row), True)
        self.assertIs(eval_expr(IsNull(col("b", "y")), self.row), False)
        self.assertIs(
            eval_expr(IsNull(col("b", "x"), negated=True), self.row), False
        )
        self.assertIs(
            eval_expr(IsNull(col("b", "y"), negated=True), self.row), True
        )

    def test_not_truth_table(self) -> None:
        for value, expected in [(True, False), (False, True), (None, None)]:
            with self.subTest(value=value):
                row = {("a", "x"): value}
                self.assertIs(
                    eval_expr(Not(col("a", "x")), row), expected
                )

    def test_and_truth_table(self) -> None:
        cases = [
            ([True, True], True),
            ([True, False], False),
            ([False, None], False),       # FALSE dominates
            ([True, None], None),         # UNKNOWN survives
            ([None, None], None),
        ]
        for values, expected in cases:
            with self.subTest(values=values):
                row = {("a", f"x{i}"): v for i, v in enumerate(values)}
                expr = And([col("a", f"x{i}") for i in range(len(values))])
                self.assertIs(eval_expr(expr, row), expected)

    def test_or_truth_table(self) -> None:
        cases = [
            ([False, False], False),
            ([True, None], True),         # TRUE dominates
            ([False, None], None),
            ([None, None], None),
        ]
        for values, expected in cases:
            with self.subTest(values=values):
                row = {("a", f"x{i}"): v for i, v in enumerate(values)}
                expr = Or([col("a", f"x{i}") for i in range(len(values))])
                self.assertIs(eval_expr(expr, row), expected)

    def test_or_with_is_null_keeps_padding_row_unknown_path(self) -> None:
        # (b.x = 1 OR b.x IS NULL): on a NULL-padded row the first disjunct
        # is UNKNOWN and the second TRUE -> TRUE, so padding rows survive.
        expr = Or(
            [Compare(col("b", "x"), "=", Literal(1)), IsNull(col("b", "x"))]
        )
        padded = {("b", "x"): None}
        self.assertIs(eval_expr(expr, padded), True)
        self.assertFalse(is_null_rejecting(expr, ["b"]))

    def test_or_without_null_branch_rejects_padding_row(self) -> None:
        # (b.x = 1 OR b.y = 2): on padding both are UNKNOWN -> UNKNOWN,
        # so a WHERE filter drops the padding row: null-rejecting.
        expr = Or(
            [
                Compare(col("b", "x"), "=", Literal(1)),
                Compare(col("b", "y"), "=", Literal(2)),
            ]
        )
        padded = {("b", "x"): None, ("b", "y"): None}
        self.assertIsNone(eval_expr(expr, padded))
        self.assertTrue(is_null_rejecting(expr, ["b"]))


class NullRejectionProofTests(unittest.TestCase):
    def test_simple_comparison_is_null_rejecting(self) -> None:
        expr = Compare(col("b", "x"), ">", Literal(5))
        self.assertTrue(is_null_rejecting(expr, ["b"]))
        self.assertIn("UNKNOWN", null_status_explanation(expr, ["b"]))

    def test_is_null_is_not_null_rejecting(self) -> None:
        expr = IsNull(col("b", "x"))
        self.assertFalse(is_null_rejecting(expr, ["b"]))
        self.assertIn("TRUE", null_status_explanation(expr, ["b"]))

    def test_is_not_null_is_null_rejecting(self) -> None:
        expr = IsNull(col("b", "x"), negated=True)
        self.assertTrue(is_null_rejecting(expr, ["b"]))

    def test_negated_is_null_rejects(self) -> None:
        expr = Not(IsNull(col("b", "x")))
        self.assertTrue(is_null_rejecting(expr, ["b"]))

    def test_mixed_alias_expression_is_unprovable(self) -> None:
        # Compares a null-padded b column with a free a column: the prover
        # must return _FREE rather than guess, so the predicate is retained.
        expr = Compare(col("b", "x"), "=", col("a", "x"))
        self.assertFalse(is_null_rejecting(expr, ["b"]))
        self.assertIn(
            "not provable", null_status_explanation(expr, ["b"])
        )

    def test_conjunction_overall_rejects_even_if_one_conjunct_does_not(self) -> None:
        # r.x > 5 AND r.y IS NULL: individually the IS NULL is not
        # null-rejecting, but the conjunction is provably FALSE/UNKNOWN on
        # padding (UNKNOWN AND TRUE -> UNKNOWN).
        expr = And(
            [
                Compare(col("r", "x"), ">", Literal(5)),
                IsNull(col("r", "y")),
            ]
        )
        self.assertTrue(is_null_rejecting(expr, ["r"]))

    def test_conjunction_with_is_null_only_does_not_reject(self) -> None:
        expr = And([IsNull(col("r", "x")), IsNull(col("r", "y"))])
        self.assertFalse(is_null_rejecting(expr, ["r"]))

    def test_free_value_inside_is_null_is_undecidable(self) -> None:
        # b is null-padded; a.x belongs to the preserved side and keeps its
        # actual value -- which may itself be NULL. Hence a.x IS NULL cannot
        # be decided from the padding information alone and the prover must
        # return the _FREE ("not provable") verdict rather than guess.
        expr = IsNull(col("a", "x"))
        self.assertFalse(is_null_rejecting(expr, ["b"]))
        self.assertIn(
            "not provable", null_status_explanation(expr, ["b"])
        )
        # The same conservatism applies to IS NOT NULL.
        self.assertFalse(
            is_null_rejecting(IsNull(col("a", "x"), negated=True), ["b"])
        )

    def test_left_columns_do_not_participate_in_padding(self) -> None:
        expr = Compare(col("a", "x"), ">", Literal(0))
        # a is the preserved side: its columns stay free, and the predicate
        # references no null-supplied alias at all.
        self.assertFalse(is_null_rejecting(expr, ["b"]))


class StructuralHelperTests(unittest.TestCase):
    def test_split_and_recombine_flattens_nested_and(self) -> None:
        e1 = Compare(col("a", "x"), "=", Literal(1))
        e2 = IsNull(col("b", "y"))
        e3 = Compare(col("b", "z"), "<", Literal(9))
        flat = split_conjuncts(And([And([e1, e2]), e3]))
        self.assertEqual(flat, [e1, e2, e3])
        recombined = and_exprs(flat)
        self.assertIsInstance(recombined, And)
        self.assertEqual(recombined.parts, (e1, e2, e3))

    def test_referenced_aliases_nested(self) -> None:
        expr = And(
            [
                Compare(col("a", "x"), "=", col("b", "x")),
                Or([IsNull(col("c", "y")), Compare(col("a", "z"), "<>", Literal(2))]),
            ]
        )
        self.assertEqual(referenced_aliases(expr), frozenset({"a", "b", "c"}))

    def test_text_rendering(self) -> None:
        expr = And(
            [
                Compare(col("a", "x"), "=", Literal(1)),
                Or([IsNull(col("b", "y")), Not(Compare(col("b", "z"), ">", Literal(3)))]),
            ]
        )
        text = expr_to_text(expr)
        self.assertIn("a.x = 1", text)
        self.assertIn("b.y IS NULL", text)
        self.assertIn("NOT", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
