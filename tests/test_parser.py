from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pruning import ColumnStats, parse_expr, prune  # noqa: E402
from pruning.expressions import Compare, IsNull  # noqa: E402
from pruning.parser import ExpressionParseError  # noqa: E402


class ParserTests(unittest.TestCase):
    def test_parses_comparisons_and_keywords_case_insensitively(self):
        expr = parse_expr("X > 10 aNd y <= -3")
        self.assertIn("And", type(expr).__name__)

    def test_parses_is_null_forms(self):
        self.assertIsInstance(parse_expr("x IS NULL"), IsNull)
        self.assertIsInstance(parse_expr("x is not null"), IsNull)
        self.assertTrue(parse_expr("x IS NOT NULL").negated)
        self.assertIsInstance(parse_expr("ISNULL(x)"), IsNull)

    def test_parses_parentheses_and_operators(self):
        expr = parse_expr("(x = 1 OR x == 2) AND NOT y <> 3")
        result = prune(
            expr,
            {
                "x": ColumnStats(1, 2, False),
                "y": ColumnStats(3, 3, False),
            },
        )
        # x can be 1 or 2, and y=4 makes y<>3 true; however x can also be only
        # one value per row, so the OR is true for both values. Whole group?
        # y fixed 4 and x in [1,2] all satisfy x=1 OR x=2 due integer domain.
        self.assertTrue(result.all_passed)

    def test_invalid_expression_raises_parse_error(self):
        with self.assertRaises(ExpressionParseError):
            parse_expr("x >")
        with self.assertRaises(ExpressionParseError):
            parse_expr("")

    def test_arithmetic_offset_is_supported(self):
        result = prune(
            "x + 1 < 10",
            {"x": ColumnStats(0, 8, False)},
        )
        self.assertTrue(result.all_passed)
        impossible = prune(
            "x + 1 > 100",
            {"x": ColumnStats(0, 8, False)},
        )
        self.assertTrue(impossible.skipped)


if __name__ == "__main__":
    unittest.main()
