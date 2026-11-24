from __future__ import annotations

import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pruning import ColumnStats, prune  # noqa: E402


UNKNOWN = object()


def tri_not(value):
    if value is UNKNOWN:
        return UNKNOWN
    return not value


def tri_and(left, right):
    if left is False or right is False:
        return False
    if left is True and right is True:
        return True
    return UNKNOWN


def tri_or(left, right):
    if left is True or right is True:
        return True
    if left is False and right is False:
        return False
    return UNKNOWN


def evaluate(expr, row):
    tag = expr[0]
    if tag in ("=", "!=", "<", "<=", ">", ">="):
        left = row[expr[1]] if isinstance(expr[1], str) else expr[1]
        right = row[expr[2]] if isinstance(expr[2], str) else expr[2]
        if left is None or right is None:
            return UNKNOWN
        if tag == "=":
            return left == right
        if tag == "!=":
            return left != right
        if tag == "<":
            return left < right
        if tag == "<=":
            return left <= right
        if tag == ">":
            return left > right
        return left >= right
    if tag == "is_null":
        return row[expr[1]] is None
    if tag == "is_not_null":
        return row[expr[1]] is not None
    if tag == "not":
        return tri_not(evaluate(expr[1], row))
    left = evaluate(expr[1], row)
    right = evaluate(expr[2], row)
    return tri_and(left, right) if tag == "and" else tri_or(left, right)


def stats_from_rows(rows, name):
    values = [row[name] for row in rows]
    non_null = [value for value in values if value is not None]
    return ColumnStats(
        lower=min(non_null) if non_null else None,
        upper=max(non_null) if non_null else None,
        may_be_null=any(value is None for value in values),
        all_null=not non_null,
    )


def atom(rng):
    op = rng.choice(("=", "!=", "<", "<=", ">", ">="))
    if rng.random() < 0.7:
        column = rng.choice(("x", "y"))
        return (op, column, rng.randint(-4, 5))
    return (op, "x", "y")


def expression(rng, depth=0):
    if depth >= 3 or rng.random() < 0.4:
        if rng.random() < 0.25:
            return (rng.choice(("is_null", "is_not_null")), rng.choice(("x", "y")))
        return atom(rng)
    if rng.random() < 0.2:
        return ("not", expression(rng, depth + 1))
    tag = rng.choice(("and", "or"))
    return (tag, expression(rng, depth + 1), expression(rng, depth + 1))


class SyntheticRowGroupSoundnessTests(unittest.TestCase):
    def test_pruning_decisions_are_safe_for_random_sparse_row_groups(self):
        rng = random.Random(20261125)
        for case in range(300):
            row_count = rng.randint(1, 12)
            rows = []
            for _ in range(row_count):
                row = {}
                for name in ("x", "y"):
                    if rng.random() < 0.25:
                        row[name] = None
                    else:
                        row[name] = rng.randint(-2, 4)
                rows.append(row)

            column_stats = {
                "x": stats_from_rows(rows, "x"),
                "y": stats_from_rows(rows, "y"),
            }
            expr = expression(rng)
            result = prune(expr, column_stats)
            true_count = sum(evaluate(expr, row) is True for row in rows)

            with self.subTest(case=case, expr=expr, rows=rows):
                if result.skipped:
                    self.assertEqual(true_count, 0, msg="SKIP must not hide TRUE rows")
                elif result.all_passed:
                    self.assertEqual(
                        true_count,
                        len(rows),
                        msg="PASS is allowed only when every actual row is TRUE",
                    )
                else:
                    self.assertTrue(result.needs_read)
                self.assertTrue(result.reasons)
                # Evidence must remain JSON-like/reviewable.
                result.to_dict()

    def test_confirmed_relation_dataset(self):
        rng = random.Random(4242)
        rows = []
        for _ in range(20):
            if rng.random() < 0.2:
                rows.append({"x": None, "y": rng.randint(0, 9)})
            elif rng.random() < 0.2:
                rows.append({"x": rng.randint(0, 9), "y": None})
            else:
                x = rng.randint(0, 8)
                rows.append({"x": x, "y": x + rng.randint(1, 4)})

        # Confirm the relation independently before handing it to the pruner.
        self.assertTrue(
            all(
                row["x"] < row["y"]
                for row in rows
                if row["x"] is not None and row["y"] is not None
            )
        )
        column_stats = {
            "x": stats_from_rows(rows, "x"),
            "y": stats_from_rows(rows, "y"),
        }

        skip = prune(
            "x > 100 AND y < 0",
            column_stats,
            relationships=["x < y"],
        )
        self.assertTrue(skip.skipped)
        self.assertEqual(sum(evaluate(("and", (">", "x", 100), ("<", "y", 0)), r) is True for r in rows), 0)

        maybe = prune("x < y", column_stats, relationships=["x < y"])
        # Because NULLs may exist, SQL result can be UNKNOWN for some rows.
        self.assertTrue(maybe.needs_read)


if __name__ == "__main__":
    unittest.main()
