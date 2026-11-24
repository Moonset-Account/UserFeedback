from __future__ import annotations

import itertools
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pruning import ColumnStats, prune  # noqa: E402


UNKNOWN = object()

VALUE_DOMAINS = [
    (0, 2, False, False),
    (-2, 2, False, False),
    (0, 2, True, False),
    (-1, 1, True, False),
    (None, None, True, True),
]

OPERATORS = ("=", "!=", "<", "<=", ">", ">=")
RELATIONS = (None, "x < y", "x <= y", "x = y", "x > y")


def values_for(spec):
    lower, upper, nullable, all_null = spec
    if all_null:
        return [None]
    values = list(range(lower, upper + 1))
    if nullable:
        values.append(None)
    return values


def stats_for(spec):
    lower, upper, nullable, all_null = spec
    return ColumnStats(lower, upper, nullable, all_null)


def compare_values(left, op, right):
    if op == "=":
        return left == right
    if op == "!=":
        return left != right
    if op == "<":
        return left < right
    if op == "<=":
        return left <= right
    if op == ">":
        return left > right
    if op == ">=":
        return left >= right
    raise AssertionError(op)


def eval_expr(expr, row):
    tag = expr[0]
    if tag in OPERATORS:
        left = row[expr[1]] if isinstance(expr[1], str) else expr[1]
        right = row[expr[2]] if isinstance(expr[2], str) else expr[2]
        if left is None or right is None:
            return UNKNOWN
        return compare_values(left, tag, right)

    if tag == "is_null":
        return row[expr[1]] is None
    if tag == "is_not_null":
        return row[expr[1]] is not None

    if tag == "not":
        value = eval_expr(expr[1], row)
        if value is UNKNOWN:
            return UNKNOWN
        return not value

    left = eval_expr(expr[1], row)
    right = eval_expr(expr[2], row)
    if tag == "and":
        if left is False or right is False:
            return False
        if left is True and right is True:
            return True
        return UNKNOWN
    if tag == "or":
        if left is True or right is True:
            return True
        if left is False and right is False:
            return False
        return UNKNOWN
    raise AssertionError(f"bad expression {expr!r}")


def relation_allows(row, relation):
    if relation is None:
        return True
    op = relation.split()[1]
    return compare_values(row["x"], op, row["y"]) if row["x"] is not None and row["y"] is not None else True


def random_atom(rng):
    kind = rng.randrange(5)
    op = rng.choice(OPERATORS)
    if kind == 0:
        return (op, "x", rng.choice(range(-3, 4)))
    if kind == 1:
        return (op, "y", rng.choice(range(-3, 4)))
    if kind == 2:
        return (op, "x", "y")
    if kind == 3:
        return (rng.choice(("is_null", "is_not_null")), "x")
    return (rng.choice(("is_null", "is_not_null")), "y")


def random_expr(rng, depth=0):
    if depth >= 3 or rng.random() < 0.45:
        return random_atom(rng)
    choice = rng.randrange(3)
    if choice == 0:
        return ("not", random_expr(rng, depth + 1))
    tag = "and" if choice == 1 else "or"
    return (tag, random_expr(rng, depth + 1), random_expr(rng, depth + 1))


class ExhaustiveAbstractOracleTests(unittest.TestCase):
    def assert_matches_oracle(self, expr, x_spec, y_spec, relation):
        x_values = values_for(x_spec)
        y_values = values_for(y_spec)
        rows = []
        for x_value, y_value in itertools.product(x_values, y_values):
            row = {"x": x_value, "y": y_value}
            if relation_allows(row, relation):
                rows.append(row)

        self.assertTrue(rows, "oracle universe should not be empty")
        outcomes = [eval_expr(expr, row) for row in rows]
        any_true = any(value is True for value in outcomes)
        all_true = all(value is True for value in outcomes)
        expected = "skip" if not any_true else "pass" if all_true else "read"

        column_stats = {"x": stats_for(x_spec), "y": stats_for(y_spec)}
        relationships = [] if relation is None else [relation]
        result = prune(expr, column_stats, relationships)

        self.assertEqual(
            result.decision.value,
            expected,
            msg=(
                f"expr={expr!r}\nx={x_spec}\ny={y_spec}\nrel={relation}\n"
                f"expected={expected}, got={result.decision.value}\n"
                + "\n".join(result.reasons)
            ),
        )

    def test_all_atoms_across_all_small_domains(self):
        atoms = []
        for op in OPERATORS:
            for constant in range(-3, 4):
                atoms.append((op, "x", constant))
                atoms.append((op, "y", constant))
            atoms.append((op, "x", "y"))
        atoms.extend([("is_null", "x"), ("is_not_null", "x")])
        atoms.extend([("is_null", "y"), ("is_not_null", "y")])

        for x_spec in VALUE_DOMAINS:
            for y_spec in VALUE_DOMAINS:
                for relation in RELATIONS:
                    for atom in atoms:
                        self.assert_matches_oracle(atom, x_spec, y_spec, relation)

    def test_random_nested_expressions_match_oracle(self):
        rng = random.Random(16696)
        for case_index in range(600):
            x_spec = rng.choice(VALUE_DOMAINS)
            y_spec = rng.choice(VALUE_DOMAINS)
            relation = rng.choice(RELATIONS)
            expr = random_expr(rng)
            with self.subTest(case=case_index, expr=expr):
                self.assert_matches_oracle(expr, x_spec, y_spec, relation)


if __name__ == "__main__":
    unittest.main()
