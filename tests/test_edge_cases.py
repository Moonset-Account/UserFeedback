"""Edge-case and defensive-branch tests.

These cover constructor validation, operator overloads, constant-only
filters above outer joins, conversions with no remaining predicate, stacked
filter merging and diagnostics rendering -- the branches the main scenario
suites do not hit.
"""

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
    expr_to_text,
)
from predrewrite.plans import (
    Filter,
    InnerJoin,
    LeftOuterJoin,
    Scan,
    Table,
    plan_to_text,
)
from predrewrite.rewrite import rewrite


def col(alias: str, name: str) -> Column:
    return Column(alias, name)


def two_scan_loj() -> LeftOuterJoin:
    t_a = Table("A", ("k", "x"), ({"k": 1, "x": 1},))
    t_b = Table("B", ("k", "y"), ({"k": 1, "y": 2},))
    a = Scan(alias="a", table=t_a, node_id="scan_a")
    b = Scan(alias="b", table=t_b, node_id="scan_b")
    return LeftOuterJoin(
        left=a, right=b,
        on=Compare(col("a", "k"), "=", col("b", "k")),
        node_id="loj",
    )


class ExpressionConstructorTests(unittest.TestCase):
    def test_operator_overloads_build_flat_expressions(self) -> None:
        e1 = Compare(col("a", "x"), "=", Literal(1))
        e2 = IsNull(col("b", "y"))
        e3 = Compare(col("b", "z"), ">", Literal(3))

        combined_and = e1 & e2
        self.assertIsInstance(combined_and, And)
        self.assertEqual(combined_and.parts, (e1, e2))

        combined_or = e1 | e2
        self.assertIsInstance(combined_or, Or)
        self.assertEqual(combined_or.parts, (e1, e2))
        # __or__ flattens a nested Or on either side.
        flat_or = combined_or | e3
        self.assertEqual(flat_or.parts, (e1, e2, e3))

        self.assertEqual(~e1, Not(e1))

    def test_invalid_comparison_operator_rejected(self) -> None:
        with self.assertRaises(ValueError):
            Compare(col("a", "x"), "==", Literal(1))

    def test_empty_and_or_and_exprs_rejected(self) -> None:
        with self.assertRaises(ValueError):
            And([])
        with self.assertRaises(ValueError):
            Or([])
        with self.assertRaises(ValueError):
            and_exprs([])

    def test_and_exprs_single_item_returns_it_unchanged(self) -> None:
        e = IsNull(col("a", "x"))
        self.assertIs(and_exprs([e]), e)

    def test_nested_and_or_flatten_in_constructor(self) -> None:
        e1 = Compare(col("a", "x"), "=", Literal(1))
        e2 = IsNull(col("b", "y"))
        e3 = Compare(col("b", "z"), "<", Literal(9))
        self.assertEqual(And([And([e1, e2]), e3]).parts, (e1, e2, e3))
        self.assertEqual(Or([Or([e1, e2]), e3]).parts, (e1, e2, e3))

    def test_literal_null_renders_as_null(self) -> None:
        self.assertEqual(expr_to_text(Literal(None)), "NULL")
        self.assertEqual(expr_to_text(Literal(7)), "7")


class RewriteDefensiveBranchTests(unittest.TestCase):
    def test_constant_filter_above_outer_join_is_retained(self) -> None:
        # A predicate referencing no alias at all lands in keep_items at an
        # outer join and must remain above it.
        plan = Filter(
            source=two_scan_loj(),
            expr=Literal(True),
            pids=("c1",),
            node_id="where",
        )
        result = rewrite(plan)
        self.assertEqual(result.conversion_events, [])
        event = result.predicate_events[0]
        self.assertEqual(event.action, "retained")
        self.assertIn("constant", event.reason)
        # The outer join survives and the filter still sits above it.
        text = plan_to_text(result.plan)
        self.assertIn("LeftOuterJoin", text)
        self.assertTrue(text.startswith("Filter"))

    def test_conversion_with_no_remaining_predicate_returns_bare_inner(self) -> None:
        # A single right-side conjunct converts the LOJ and then pushes all
        # the way to the right scan, so no filter remains above the new
        # inner join (the `return inner` branch).
        plan = Filter(
            source=two_scan_loj(),
            expr=Compare(col("b", "y"), ">", Literal(0)),
            pids=("p1",),
            node_id="where",
        )
        result = rewrite(plan)
        self.assertEqual(len(result.conversion_events), 1)
        top = result.plan
        self.assertIsInstance(top, InnerJoin)
        self.assertNotIsInstance(top, Filter)
        # The predicate ended on a filter directly above the b scan.
        right = top.right
        self.assertIsInstance(right, Filter)
        self.assertIsInstance(right.source, Scan)
        self.assertEqual(right.source.alias, "b")

    def test_stacked_filters_merge_through_a_join(self) -> None:
        # Two stacked filters above an inner join must collapse to a single
        # filter node per hosting location (the Filter-source branch).
        t_a = Table("A", ("k", "x"), ({"k": 1, "x": 1},))
        t_b = Table("B", ("k", "y"), ({"k": 1, "y": 2},))
        a = Scan(alias="a", table=t_a, node_id="scan_a")
        b = Scan(alias="b", table=t_b, node_id="scan_b")
        join = InnerJoin(
            left=a, right=b,
            on=Compare(col("a", "k"), "=", col("b", "k")),
            node_id="ij",
        )
        lower = Filter(
            source=join,
            expr=Compare(col("a", "x"), ">", Literal(0)),
            pids=("p1",),
            node_id="f_lower",
        )
        upper = Filter(
            source=lower,
            expr=Compare(col("a", "x"), "<", Literal(9)),
            pids=("p2",),
            node_id="f_upper",
        )
        result = rewrite(upper)

        def count_filters(node) -> int:
            from predrewrite.plans import Plan
            if isinstance(node, Scan):
                return 0
            if isinstance(node, Filter):
                return 1 + count_filters(node.source)
            return count_filters(node.left) + count_filters(node.right)

        # Both a-side predicates merge into one filter above the a scan.
        self.assertEqual(count_filters(result.plan), 1)
        dest = result.destinations()
        self.assertEqual(dest["p1"], dest["p2"])
        # Both ids survive the merge.
        hosted = result.plan
        while not isinstance(hosted, Filter):
            hosted = hosted.left
        self.assertEqual(set(hosted.pids), {"p1", "p2"})

    def test_mismatched_pid_count_synthesizes_ids_instead_of_crashing(self) -> None:
        # A filter whose pid list is shorter than its conjunct list (can
        # occur for a hand-built or re-rewritten plan) is handled gracefully.
        plan = Filter(
            source=two_scan_loj(),
            expr=And(
                [
                    Compare(col("a", "x"), ">", Literal(0)),
                    IsNull(col("b", "y")),
                ]
            ),
            pids=("only_one",),  # deliberately too short
            node_id="where",
        )
        result = rewrite(plan)
        destinations = result.destinations()
        self.assertIn("only_one", destinations)
        synthesized = [pid for pid in destinations if pid.startswith("p_")]
        self.assertEqual(len(synthesized), 1)

    def test_render_filter_with_pids_and_all_node_kinds(self) -> None:
        plan = Filter(
            source=InnerJoin(
                left=Scan(alias="a", table=Table("A", ("k",), ({"k": 1},)),
                           node_id="sa"),
                right=Scan(alias="b", table=Table("B", ("k",), ({"k": 1},)),
                            node_id="sb"),
                on=Compare(col("a", "k"), "=", col("b", "k")),
                node_id="ij",
            ),
            expr=And([IsNull(col("b", "k")), Not(Compare(col("a", "k"), ">", Literal(2)))]),
            pids=("p1", "p2"),
            node_id="f",
        )
        text = plan_to_text(plan)
        self.assertIn("<- p1, p2", text)
        self.assertIn("InnerJoin", text)
        self.assertIn("NOT", text)

    def test_render_filter_without_pids_omits_suffix(self) -> None:
        # Covers the plan_to_text branch where a Filter carries no pids.
        scan = Scan(alias="a", table=Table("A", ("k",), ({"k": 1},)),
                    node_id="sa")
        flt = Filter(source=scan, expr=Literal(True), pids=(), node_id="f0")
        text = plan_to_text(flt)
        self.assertIn("Filter [f0] True", text)
        self.assertNotIn("<-", text)

    def test_conversion_consumes_every_conjunct_with_no_remainder(self) -> None:
        # Right-side-only filter on a LOJ whose right child is a plain scan:
        # after conversion the single conjunct pushes onto that scan, so the
        # inner join is returned directly with no filter above it.
        t_a = Table("A", ("k",), ({"k": 1},))
        t_b = Table("B", ("k", "y"), ({"k": 1, "y": 2},))
        a = Scan(alias="a", table=t_a, node_id="scan_a")
        b = Scan(alias="b", table=t_b, node_id="scan_b")
        loj = LeftOuterJoin(
            left=a, right=b,
            on=Compare(col("a", "k"), "=", col("b", "k")),
            node_id="loj",
        )
        # Add an explicit left-only predicate too, routed into the left
        # subtree, leaving zero items above the converted join.
        plan = Filter(
            source=loj,
            expr=Compare(col("b", "y"), "<>", Literal(0)),
            pids=("p1",),
            node_id="where",
        )
        result = rewrite(plan)
        self.assertIsInstance(result.plan, InnerJoin)
        self.assertNotIsInstance(result.plan, Filter)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
