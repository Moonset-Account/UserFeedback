"""Exhaustive small-table equivalence tests with duplicate rows.

For every scenario we enumerate *all* tables over a tiny value domain
(including NULL and repeated rows), build the original plan and the rewritten
plan, evaluate both with bag semantics and assert the output multisets are
identical. This directly checks that padding rows, duplicate rows and
three-valued logic results are preserved through every rewrite decision.
"""

import itertools
import unittest

from predrewrite.expressions import (
    And,
    Column,
    Compare,
    Expr,
    IsNull,
    Literal,
    Not,
    Or,
)
from predrewrite.plans import (
    Filter,
    InnerJoin,
    LeftOuterJoin,
    Plan,
    Scan,
    Table,
    canonical_bag,
    evaluate_plan,
    plan_to_text,
)
from predrewrite.rewrite import rewrite

# Domain small enough for full enumeration: NULL plus one concrete value.
# Every table is enumerated exhaustively over this domain with 0-2 rows,
# so duplicate rows, empty tables and every NULL/value placement are covered.
DOMAIN: tuple = (None, 1)


def col(alias: str, name: str) -> Column:
    return Column(alias, name)


def all_tables(name: str, columns: tuple[str, ...]) -> list[Table]:
    """Every 0- to 2-row table over DOMAIN for the given columns."""
    row_dicts = [
        dict(zip(columns, values))
        for values in itertools.product(DOMAIN, repeat=len(columns))
    ]
    tables: list[Table] = []
    for size in range(0, 3):
        for combo in itertools.product(range(len(row_dicts)), repeat=size):
            tables.append(
                Table(
                    name=name,
                    columns=columns,
                    rows=tuple(row_dicts[i] for i in combo),
                )
            )
    return tables


def assert_bag_equivalent(
    testcase: unittest.TestCase, original: Plan, label: str
) -> None:
    result = rewrite(original)
    before = canonical_bag(evaluate_plan(original))
    after = canonical_bag(evaluate_plan(result.plan))
    testcase.assertEqual(
        before,
        after,
        msg=(
            f"bag mismatch for {label}\n--- original ---\n"
            f"{plan_to_text(original)}\n--- rewritten ---\n"
            f"{plan_to_text(result.plan)}"
        ),
    )


class RightNestedTwoLevelLojTests(unittest.TestCase):
    """A LOJ (LOJ A B) C, i.e. the required double left-outer-join shape."""

    def setUp(self) -> None:
        self.a_tables = all_tables("A", ("k", "x"))
        self.b_tables = all_tables("B", ("k", "y"))
        self.c_tables = all_tables("C", ("k", "z"))
        # Domain {NULL, 1} over 2 columns gives 4 distinct rows; tables of
        # 0..2 rows drawn with replacement -> 1 + 4 + 16 = 21 variants, so
        # the full grid is 21^3 = 9_261 table combinations per scenario.
        # Duplicate rows appear because rows are drawn with replacement.
        self.assertEqual(len(self.a_tables), 21)
        self.grid_size = len(self.a_tables) ** 3

    def _build_plan(
        self, ta: Table, tb: Table, tc: Table, where: Expr, pids: tuple[str, ...]
    ) -> Plan:
        a = Scan(alias="a", table=ta, node_id="scan_a")
        b = Scan(alias="b", table=tb, node_id="scan_b")
        c = Scan(alias="c", table=tc, node_id="scan_c")
        lower = LeftOuterJoin(
            left=a, right=b,
            on=Compare(col("a", "k"), "=", col("b", "k")),
            node_id="loj_ab",
        )
        top = LeftOuterJoin(
            left=lower, right=c,
            on=Compare(col("a", "k"), "=", col("c", "k")),
            node_id="loj_abc",
        )
        return Filter(source=top, expr=where, pids=pids, node_id="where")

    def _enumerate(self, where: Expr, pids: tuple[str, ...], label: str) -> int:
        count = 0
        for ta, tb, tc in itertools.product(
            self.a_tables, self.b_tables, self.c_tables
        ):
            plan = self._build_plan(ta, tb, tc, where, pids)
            assert_bag_equivalent(self, plan, label)
            count += 1
        return count

    def test_required_scenario_is_null_and_or_must_stay_outer(self) -> None:
        # The task's mandatory case: IS NULL + OR inside a two-level LOJ.
        # (c.z = 1 OR c.z IS NULL) is TRUE on a padding row -> not
        # null-rejecting; b.y IS NULL likewise keeps padding rows.
        where = And(
            [
                Or(
                    [
                        Compare(col("c", "z"), "=", Literal(1)),
                        IsNull(col("c", "z")),
                    ]
                ),
                IsNull(col("b", "y")),
            ]
        )
        n = self._enumerate(where, ("p_or", "p_isnull"), "isnull+or")
        self.assertEqual(n, self.grid_size)

        # And the rewrite really must keep both joins outer.
        sample = self._build_plan(
            self.a_tables[-1], self.b_tables[-1], self.c_tables[-1],
            where, ("p_or", "p_isnull"),
        )
        result = rewrite(sample)
        self.assertEqual(result.conversion_events, [])
        self.assertTrue(
            all(e.action == "retained" for e in result.predicate_events)
        )

    def test_required_similar_plan_legally_converts_to_inner(self) -> None:
        # Same two-level LOJ shape as the mandatory retain case, but the
        # filter is now null-rejecting on the *top* join's null-supplied
        # side (c) and references the lower LOJ's right side (b) only through
        # an IS NULL that keeps that lower join outer. Exactly one
        # conversion happens -- the top LOJ -> inner -- while the lower
        # A-B left outer join is preserved, mirroring the required scenario
        # as closely as possible.
        where = And(
            [
                # OR with no IS NULL branch: UNKNOWN on a c-padded row.
                Or(
                    [
                        Compare(col("c", "z"), "=", Literal(1)),
                        Compare(col("c", "k"), ">", Literal(1)),
                    ]
                ),
                # b.y IS NULL is TRUE on a b-padded row: it keeps the lower
                # LOJ outer even though it sits inside the top filter.
                IsNull(col("b", "y")),
            ]
        )
        n = self._enumerate(where, ("p_or", "p_bisnull"), "convert-or")
        self.assertEqual(n, self.grid_size)

        sample = self._build_plan(
            self.a_tables[-1], self.b_tables[-1], self.c_tables[-1],
            where, ("p_or", "p_bisnull"),
        )
        result = rewrite(sample)
        self.assertEqual(len(result.conversion_events), 1)
        conv = result.conversion_events[0]
        # Only the top join (whose null-supplied side is c) is converted,
        # and only p_or references that side. The b-side IS NULL belongs to
        # the top join's *left* subtree and is routed down to the lower LOJ,
        # where it is retained to keep that join outer.
        self.assertEqual(conv.outer_node_id, "loj_abc")
        self.assertEqual(conv.triggered_by, ("p_or",))
        b_events = result.events_for("p_bisnull")
        self.assertTrue(all(e.action == "retained" for e in b_events))
        # The lower A-B join is still a left outer join afterwards.
        from predrewrite.plans import LeftOuterJoin as _LOJ

        def _has_left_outer(plan: Plan) -> bool:
            if isinstance(plan, _LOJ):
                return True
            if isinstance(plan, Filter):
                return _has_left_outer(plan.source)
            if isinstance(plan, (InnerJoin, LeftOuterJoin)):
                return _has_left_outer(plan.left) or _has_left_outer(plan.right)
            return False

        self.assertTrue(_has_left_outer(result.plan))

    def test_two_conjunctions_both_rejecting_convert_both_joins(self) -> None:
        # Variant where both levels are null-rejecting: both LOJs convert.
        where = And(
            [
                Or(
                    [
                        Compare(col("c", "z"), "=", Literal(1)),
                        Compare(col("c", "k"), ">", Literal(1)),
                    ]
                ),
                IsNull(col("b", "y"), negated=True),
            ]
        )
        n = self._enumerate(where, ("p_or", "p_notnull"), "convert-both")
        self.assertEqual(n, self.grid_size)
        sample = self._build_plan(
            self.a_tables[-1], self.b_tables[-1], self.c_tables[-1],
            where, ("p_or", "p_notnull"),
        )
        result = rewrite(sample)
        self.assertEqual(len(result.conversion_events), 2)

    def test_cascade_conversion_both_levels(self) -> None:
        # Predicates reject padding rows of BOTH outer joins, so both must
        # convert, and pushing must continue down to the base scans.
        where = And(
            [
                Compare(col("b", "y"), ">", Literal(1)),
                Compare(col("c", "z"), "<", Literal(2)),
            ]
        )
        n = self._enumerate(where, ("p_b", "p_c"), "cascade")
        self.assertEqual(n, self.grid_size)
        sample = self._build_plan(
            self.a_tables[-1], self.b_tables[-1], self.c_tables[-1],
            where, ("p_b", "p_c"),
        )
        result = rewrite(sample)
        self.assertEqual(len(result.conversion_events), 2)

    def test_mixed_retain_and_push(self) -> None:
        # Left-only predicate pushes; right-side IS NULL keeps the join.
        where = And(
            [
                Compare(col("a", "x"), "<>", Literal(2)),
                IsNull(col("c", "k")),
            ]
        )
        n = self._enumerate(where, ("p_a", "p_c"), "mixed")
        self.assertEqual(n, self.grid_size)

    def test_not_wrapped_is_null(self) -> None:
        where = And(
            [
                Not(IsNull(col("b", "y"))),
                Not(
                    Or(
                        [
                            Compare(col("c", "z"), "=", Literal(1)),
                            IsNull(col("c", "z")),
                        ]
                    )
                ),
            ]
        )
        # NOT(UNKNOWN OR TRUE) = NOT(TRUE) = FALSE on padding -> rejecting.
        n = self._enumerate(where, ("p_nb", "p_nc"), "not-wrapped")
        self.assertEqual(n, self.grid_size)
        sample = self._build_plan(
            self.a_tables[-1], self.b_tables[-1], self.c_tables[-1],
            where, ("p_nb", "p_nc"),
        )
        result = rewrite(sample)
        self.assertEqual(len(result.conversion_events), 2)

    def test_cross_side_comparison_retained(self) -> None:
        where = Compare(col("b", "y"), "=", col("c", "z"))
        n = self._enumerate(where, ("p_cross",), "cross")
        self.assertEqual(n, self.grid_size)
        sample = self._build_plan(
            self.a_tables[-1], self.b_tables[-1], self.c_tables[-1],
            where, ("p_cross",),
        )
        result = rewrite(sample)
        self.assertEqual(result.conversion_events, [])

    def test_ordering_operators_all_equivalent(self) -> None:
        for op in ("=", "<>", "<", "<=", ">", ">="):
            with self.subTest(op=op):
                where = Compare(col("c", "z"), op, col("a", "x"))
                n = self._enumerate(where, ("p_cmp",), f"cmp-{op}")
                self.assertEqual(n, self.grid_size)


class LeftNestedTwoLevelLojTests(unittest.TestCase):
    """A LOJ A (LOJ B C): the other nesting orientation."""

    def setUp(self) -> None:
        self.a_tables = all_tables("A", ("k", "x"))
        self.b_tables = all_tables("B", ("k", "y"))
        self.c_tables = all_tables("C", ("k", "z"))
        self.grid_size = len(self.a_tables) ** 3

    def _build_plan(self, ta: Table, tb: Table, tc: Table, where: Expr) -> Plan:
        a = Scan(alias="a", table=ta, node_id="scan_a")
        b = Scan(alias="b", table=tb, node_id="scan_b")
        c = Scan(alias="c", table=tc, node_id="scan_c")
        inner_loj = LeftOuterJoin(
            left=b, right=c,
            on=Compare(col("b", "k"), "=", col("c", "k")),
            node_id="loj_bc",
        )
        top = LeftOuterJoin(
            left=a, right=inner_loj,
            on=Compare(col("a", "k"), "=", col("b", "k")),
            node_id="loj_a_bc",
        )
        return Filter(source=top, expr=where, pids=("p1", "p2"), node_id="where")

    def test_left_nested_is_null_or_and_conversion_variants(self) -> None:
        retain_where = And(
            [
                Or(
                    [
                        Compare(col("c", "z"), "=", Literal(1)),
                        IsNull(col("c", "z")),
                    ]
                ),
                IsNull(col("b", "y")),
            ]
        )
        convert_where = And(
            [
                Compare(col("b", "y"), ">", Literal(0)),
                Compare(col("c", "z"), "<=", Literal(2)),
            ]
        )
        for label, where, expect_conversions in (
            ("retain", retain_where, 0),
            ("convert", convert_where, 2),
        ):
            count = 0
            for ta, tb, tc in itertools.product(
                self.a_tables, self.b_tables, self.c_tables
            ):
                plan = self._build_plan(ta, tb, tc, where)
                assert_bag_equivalent(self, plan, f"left-nested-{label}")
                count += 1
                if count == self.grid_size:
                    result = rewrite(plan)
                    self.assertEqual(
                        len(result.conversion_events), expect_conversions
                    )
            self.assertEqual(count, self.grid_size)


class InnerJoinPushdownExhaustiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.a_tables = all_tables("A", ("k", "x"))
        self.b_tables = all_tables("B", ("k", "y"))

    def _build(self, ta: Table, tb: Table, where: Expr, pids: tuple[str, ...]) -> Plan:
        a = Scan(alias="a", table=ta, node_id="scan_a")
        b = Scan(alias="b", table=tb, node_id="scan_b")
        join = InnerJoin(
            left=a, right=b,
            on=Compare(col("a", "k"), "=", col("b", "k")),
            node_id="ij",
        )
        return Filter(source=join, expr=where, pids=pids, node_id="where")

    def test_mixed_single_and_cross_side_conjuncts(self) -> None:
        where = And(
            [
                Compare(col("a", "x"), ">=", Literal(1)),
                IsNull(col("b", "y"), negated=True),
                Or(
                    [
                        Compare(col("a", "x"), "=", col("b", "y")),
                        Compare(col("a", "k"), "<>", col("b", "k")),
                    ]
                ),
            ]
        )
        count = 0
        for ta, tb in itertools.product(self.a_tables, self.b_tables):
            assert_bag_equivalent(
                self, self._build(ta, tb, where, ("pa", "pb", "px")),
                "inner-mixed",
            )
            count += 1
        self.assertEqual(count, 21 * 21)

    def test_empty_tables_are_handled(self) -> None:
        empty = Table("A", ("k", "x"), ())
        nonempty = self.a_tables[-1]
        where = Compare(col("a", "x"), "=", Literal(1))
        for ta, tb in ((empty, nonempty), (nonempty, empty), (empty, empty)):
            plan = self._build(ta, tb, where, ("p1",))
            assert_bag_equivalent(self, plan, "empty-table")
            self.assertEqual(evaluate_plan(rewrite(plan).plan), [])


class DuplicateRowExplicitTests(unittest.TestCase):
    """A hand-built fixture where duplicates are known and counted."""

    def test_duplicates_survive_outer_to_inner_conversion(self) -> None:
        ta = Table("A", ("k", "x"), ({"k": 1, "x": 1}, {"k": 1, "x": 1}))
        tc = Table(
            "C", ("k", "z"),
            ({"k": 1, "z": 5}, {"k": 1, "z": 5}, {"k": 2, "z": 0}),
        )
        a = Scan(alias="a", table=ta, node_id="scan_a")
        c = Scan(alias="c", table=tc, node_id="scan_c")
        loj = LeftOuterJoin(
            left=a, right=c,
            on=Compare(col("a", "k"), "=", col("c", "k")),
            node_id="loj",
        )
        plan = Filter(
            source=loj,
            expr=Compare(col("c", "z"), ">", Literal(0)),
            pids=("p1",),
        )
        result = rewrite(plan)
        self.assertEqual(len(result.conversion_events), 1)
        # 2 left copies x 2 matching right copies = 4 identical output rows;
        # the k=2 right row and both padding rows are excluded by the filter.
        bag = canonical_bag(evaluate_plan(result.plan))
        self.assertEqual(sum(bag.values()), 4)
        self.assertEqual(
            bag,
            canonical_bag(evaluate_plan(plan)),
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
