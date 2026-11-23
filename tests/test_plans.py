"""Tests for the bag-semantics evaluator (padding rows, duplicates, 3VL)."""

import unittest

from predrewrite.expressions import (
    And,
    Column,
    Compare,
    IsNull,
    Literal,
    Or,
)
from predrewrite.plans import (
    Filter,
    InnerJoin,
    LeftOuterJoin,
    Scan,
    Table,
    canonical_bag,
    evaluate_plan,
    plan_schema,
    plan_to_text,
)


def col(alias: str, name: str) -> Column:
    return Column(alias, name)


def make_tables() -> dict[str, Table]:
    # Deliberately small, with duplicate rows and NULLs so that bag
    # semantics and three-valued logic are exercised directly.
    left = Table(
        name="L",
        columns=("k", "v"),
        rows=(
            {"k": 1, "v": 10},
            {"k": 1, "v": 10},   # exact duplicate
            {"k": 2, "v": 20},
            {"k": 3, "v": None}, # unmatched left row carrying a NULL
        ),
    )
    right = Table(
        name="R",
        columns=("k", "w"),
        rows=(
            {"k": 1, "w": 100},
            {"k": 1, "w": 100},  # exact duplicate
            {"k": 2, "w": None}, # matches only under an IS-NULL-tolerant ON
        ),
    )
    return {"L": left, "R": right}


class EvaluatorSemanticsTests(unittest.TestCase):
    def setUp(self) -> None:
        tables = make_tables()
        self.l = Scan(alias="l", table=tables["L"], node_id="scan_l")
        self.r = Scan(alias="r", table=tables["R"], node_id="scan_r")

    def test_inner_join_preserves_duplicate_cartesian_multiplicity(self) -> None:
        join = InnerJoin(
            left=self.l,
            right=self.r,
            on=Compare(col("l", "k"), "=", col("r", "k")),
        )
        rows = evaluate_plan(join)
        bag = canonical_bag(rows)
        # k=1 appears twice on each side -> 2 x 2 = 4 copies of the row.
        key_1 = frozenset(
            {
                ("l", "k"): 1, ("l", "v"): 10,
                ("r", "k"): 1, ("r", "w"): 100,
            }.items()
        )
        self.assertEqual(bag[key_1], 4)
        # k=2 matches once; k=3 matches nothing -> absent.
        self.assertEqual(sum(bag.values()), 5)

    def test_left_outer_join_pads_unmatched_rows_with_nulls(self) -> None:
        join = LeftOuterJoin(
            left=self.l,
            right=self.r,
            on=Compare(col("l", "k"), "=", col("r", "k")),
        )
        rows = evaluate_plan(join)
        padded = [
            row for row in rows
            if row.get(("r", "k")) is None and row.get(("l", "k")) == 3
        ]
        self.assertEqual(len(padded), 1)
        self.assertIsNone(padded[0][("r", "k")])
        self.assertIsNone(padded[0][("r", "w")])
        # Left-side NULL value is distinct from right-side padding NULL.
        self.assertIsNone(padded[0][("l", "v")])
        # Total multiplicity: 4 (k=1) + 1 (k=2) + 1 padding (k=3).
        self.assertEqual(len(rows), 6)

    def test_on_unknown_does_not_match_but_where_unknown_does_not_pass(self) -> None:
        # ON l.v = r.w (note: deliberately not the join key):
        #   * the k=3 left row has l.v NULL -> every ON test is UNKNOWN, so
        #     it never matches;
        #   * the other left rows (v=10 twice, v=20) have no right row with
        #     an equal w (100, 100, NULL) -> they never match either.
        # Every left row therefore survives once as a NULL-padded row.
        join = LeftOuterJoin(
            left=self.l,
            right=self.r,
            on=Compare(col("l", "v"), "=", col("r", "w")),
        )
        rows = evaluate_plan(join)
        self.assertEqual(len(rows), len(self.l.table.rows))
        unmatched = [row for row in rows if row.get(("r", "k")) is None]
        self.assertEqual(len(unmatched), 4)

        # A post-join filter on the same comparison drops UNKNOWN rows too:
        # WHERE keeps TRUE only.
        filtered = Filter(
            source=join,
            expr=Compare(col("l", "v"), "=", col("r", "w")),
            pids=("p0",),
        )
        out = evaluate_plan(filtered)
        self.assertEqual(
            canonical_bag(out),
            canonical_bag(
                [
                    row
                    for row in rows
                    if row.get(("l", "v")) is not None
                    and row.get(("r", "w")) is not None
                    and row[("l", "v")] == row[("r", "w")]
                ]
            ),
        )
        # No row has v == w in the fixture, so the result is empty.
        self.assertEqual(out, [])

    def test_where_is_null_keeps_padding_rows(self) -> None:
        join = LeftOuterJoin(
            left=self.l,
            right=self.r,
            on=Compare(col("l", "k"), "=", col("r", "k")),
        )
        filtered = Filter(
            source=join,
            expr=IsNull(col("r", "k")),
            pids=("p0",),
        )
        rows = evaluate_plan(filtered)
        # Exactly the single unmatched left row (k=3) survives.
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][("l", "k")], 3)

    def test_filter_or_with_unknown_disjunct_uses_3vl(self) -> None:
        # r.w = 999 OR r.k IS NULL over the outer-join output:
        # matched rows: FALSE OR FALSE -> FALSE (dropped);
        # padding row: UNKNOWN OR TRUE -> TRUE (kept).
        join = LeftOuterJoin(
            left=self.l,
            right=self.r,
            on=Compare(col("l", "k"), "=", col("r", "k")),
        )
        expr = Or(
            [
                Compare(col("r", "w"), "=", Literal(999)),
                IsNull(col("r", "k")),
            ]
        )
        rows = evaluate_plan(Filter(source=join, expr=expr, pids=("p0",)))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][("l", "k")], 3)

    def test_schema_concatenation_and_text_rendering(self) -> None:
        join = InnerJoin(
            left=self.l,
            right=self.r,
            on=Compare(col("l", "k"), "=", col("r", "k")),
        )
        self.assertEqual(
            plan_schema(join),
            [("l", "k"), ("l", "v"), ("r", "k"), ("r", "w")],
        )
        text = plan_to_text(join)
        self.assertIn("InnerJoin", text)
        self.assertIn("Scan l=L", text)

    def test_and_filter_keeps_only_true(self) -> None:
        scan_rows = evaluate_plan(self.l)
        expr = And(
            [
                Compare(col("l", "k"), ">=", Literal(2)),
                IsNull(col("l", "v"), negated=True),
            ]
        )
        rows = evaluate_plan(Filter(source=self.l, expr=expr, pids=("p0",)))
        # k=2,v=20 passes; k=3,v=NULL fails IS NOT NULL; k=1 fails >= 2.
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][("l", "k")], 2)
        self.assertEqual(len(scan_rows), 4)  # source untouched


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
