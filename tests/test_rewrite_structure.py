"""Structural tests for the rewrite engine and its predicate trace."""

import unittest

from predrewrite.expressions import (
    And,
    Column,
    Compare,
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
    plan_to_text,
)
from predrewrite.rewrite import RewriteResult, rewrite


def col(alias: str, name: str) -> Column:
    return Column(alias, name)


def build_tables() -> dict[str, Table]:
    return {
        "A": Table("A", ("k", "x"), ({"k": 1, "x": 7}, {"k": 2, "x": None})),
        "B": Table("B", ("k", "y"), ({"k": 1, "y": 5}, {"k": 3, "y": None})),
        "C": Table("C", ("k", "z"), ({"k": 1, "z": 9}, {"k": 4, "z": None})),
    }


def build_three_scan_plan(
    top_kind: str,
    where_expr,
    pids: tuple[str, ...],
) -> Plan:
    """(A LOJ B) <top_kind> C with a filter above the top join."""
    t = build_tables()
    a = Scan(alias="a", table=t["A"], node_id="scan_a")
    b = Scan(alias="b", table=t["B"], node_id="scan_b")
    c = Scan(alias="c", table=t["C"], node_id="scan_c")
    lower = LeftOuterJoin(
        left=a,
        right=b,
        on=Compare(col("a", "k"), "=", col("b", "k")),
        node_id="loj_ab",
    )
    join_cls = LeftOuterJoin if top_kind == "loj" else InnerJoin
    top = join_cls(
        left=lower,
        right=c,
        on=Compare(col("a", "k"), "=", col("c", "k")),
        node_id=f"{top_kind}_abc",
    )
    return Filter(source=top, expr=where_expr, pids=pids, node_id="where")


def collect_filters(plan: Plan) -> list[Filter]:
    found: list[Filter] = []

    def walk(node: Plan) -> None:
        if isinstance(node, Filter):
            found.append(node)
            walk(node.source)
        elif isinstance(node, (InnerJoin, LeftOuterJoin)):
            walk(node.left)
            walk(node.right)

    walk(plan)
    return found


def collect_joins(plan: Plan) -> list[Plan]:
    found: list[Plan] = []

    def walk(node: Plan) -> None:
        if isinstance(node, Scan):
            return
        if isinstance(node, Filter):
            walk(node.source)
            return
        found.append(node)
        walk(node.left)
        walk(node.right)

    walk(plan)
    return found


class RewriteStructureTests(unittest.TestCase):
    # ------------------------------------------------------------------
    # The required two-level left outer join containing IS NULL and OR,
    # which must NOT be converted.
    # ------------------------------------------------------------------
    def test_two_level_loj_with_is_null_or_is_retained(self) -> None:
        # p1: OR containing an IS NULL branch -> padding rows can pass;
        # p2: IS NULL on c -> padding rows pass outright.
        p1 = Or(
            [
                Compare(col("c", "z"), "=", Literal(9)),
                IsNull(col("c", "z")),
            ]
        )
        p2 = IsNull(col("b", "y"))
        plan = build_three_scan_plan("loj", And([p1, p2]), ("p1", "p2"))
        result = rewrite(plan)

        # Both joins remain outer joins.
        joins = collect_joins(result.plan)
        self.assertTrue(all(isinstance(j, LeftOuterJoin) for j in joins))
        self.assertEqual(len(joins), 2)
        self.assertEqual(result.conversion_events, [])

        # Both predicates are retained above joins; nothing is pushed to a
        # scan of the null-supplied side. Each retained event's destination
        # is the real id of a filter node (filter_outer_*), not a scan.
        for event in result.predicate_events:
            if event.pid in ("p1", "p2"):
                self.assertEqual(event.action, "retained")
                self.assertTrue(event.destination.startswith("filter_outer"))

        # No filter may be placed directly under a null-supplied scan.
        for flt in collect_filters(result.plan):
            if isinstance(flt.source, Scan) and flt.source.alias in ("b", "c"):
                raise AssertionError(
                    "filter pushed to null-supplied scan:\n"
                    + plan_to_text(result.plan)
                )

    def test_unprovable_cross_side_or_is_retained_with_reason(self) -> None:
        # (b.y = c.z): references the null-supplied c but compares it with
        # a preserved-side column whose value is unknown to the prover.
        # The symbolic verdict is _FREE, so the predicate is retained.
        expr = Compare(col("b", "y"), "=", col("c", "z"))
        plan = build_three_scan_plan("loj", expr, ("q1",))
        result = rewrite(plan)
        self.assertEqual(result.conversion_events, [])
        event = next(e for e in result.predicate_events if e.pid == "q1")
        self.assertEqual(event.action, "retained")
        self.assertIn("not provably null-rejecting", event.reason)
        self.assertIn("not provable from the expression alone", event.reason)

    def test_true_on_padding_or_is_retained_with_reason(self) -> None:
        # (c.z = 1 OR c.k IS NULL): on a c-padded row the first disjunct is
        # UNKNOWN and the second TRUE, so the OR is TRUE -- padding rows
        # survive and the join must stay outer with a concrete verdict.
        expr = Or(
            [
                Compare(col("c", "z"), "=", Literal(1)),
                IsNull(col("c", "k")),
            ]
        )
        plan = build_three_scan_plan("loj", expr, ("q2",))
        result = rewrite(plan)
        self.assertEqual(result.conversion_events, [])
        event = next(e for e in result.predicate_events if e.pid == "q2")
        self.assertEqual(event.action, "retained")
        self.assertIn("not provably null-rejecting", event.reason)
        self.assertIn("TRUE on null-padded rows", event.reason)

    # ------------------------------------------------------------------
    # The similar plan that legally converts the top LOJ to an inner join.
    # ------------------------------------------------------------------
    def test_loj_legally_converts_to_inner_and_continues_pushdown(self) -> None:
        # c.z > 5 is UNKNOWN on padding -> the top LOJ over C becomes inner.
        # Afterwards the predicate is single-side (c) and pushes below the
        # (new) inner join onto the c scan.
        expr = Compare(col("c", "z"), ">", Literal(5))
        plan = build_three_scan_plan("loj", expr, ("p1",))
        result = rewrite(plan)

        self.assertEqual(len(result.conversion_events), 1)
        conv = result.conversion_events[0]
        self.assertEqual(conv.outer_node_id, "loj_abc")
        self.assertEqual(conv.triggered_by, ("p1",))
        self.assertIn("UNKNOWN", conv.justification)

        joins = collect_joins(result.plan)
        # Top join is now inner; the lower A-B join is still a left outer.
        self.assertEqual(len(joins), 2)
        kinds = {type(j).__name__ for j in joins}
        self.assertEqual(kinds, {"InnerJoin", "LeftOuterJoin"})

        # p1 ends up on a filter directly above scan c.
        filters = collect_filters(result.plan)
        hosting = [
            f for f in filters if "p1" in f.pids and isinstance(f.source, Scan)
            and f.source.alias == "c"
        ]
        self.assertEqual(len(hosting), 1)
        self.assertEqual(result.destinations()["p1"], hosting[0].node_id)

    def test_conversion_with_or_both_rejecting(self) -> None:
        # (c.z = 9 OR c.k = 4): on padding both disjuncts are UNKNOWN,
        # OR -> UNKNOWN, so the filter still rejects padding rows.
        expr = Or(
            [
                Compare(col("c", "z"), "=", Literal(9)),
                Compare(col("c", "k"), "=", Literal(4)),
            ]
        )
        plan = build_three_scan_plan("loj", expr, ("p1",))
        result = rewrite(plan)
        self.assertEqual(len(result.conversion_events), 1)
        self.assertIsInstance(
            next(j for j in collect_joins(result.plan)
                 if j.node_id == result.conversion_events[0].inner_node_id),
            InnerJoin,
        )

    def test_conjunction_proof_converts_when_one_branch_is_is_null(self) -> None:
        # c.z > 5 AND c.k IS NULL: combined proof is UNKNOWN on padding
        # (UNKNOWN AND TRUE), so conversion is still legal.
        expr = And(
            [
                Compare(col("c", "z"), ">", Literal(5)),
                IsNull(col("c", "k")),
            ]
        )
        plan = build_three_scan_plan("loj", expr, ("p1", "p2"))
        result = rewrite(plan)
        self.assertEqual(len(result.conversion_events), 1)
        self.assertEqual(
            set(result.conversion_events[0].triggered_by), {"p1", "p2"}
        )
        for event in result.predicate_events:
            self.assertEqual(event.action, "pushed")

    # ------------------------------------------------------------------
    # Inner join pushdown mechanics.
    # ------------------------------------------------------------------
    def test_inner_join_side_predicates_push_down_cross_side_stays(self) -> None:
        t = build_tables()
        a = Scan(alias="a", table=t["A"], node_id="scan_a")
        b = Scan(alias="b", table=t["B"], node_id="scan_b")
        join = InnerJoin(
            left=a, right=b,
            on=Compare(col("a", "k"), "=", col("b", "k")),
            node_id="ij",
        )
        left_only = Compare(col("a", "x"), ">", Literal(0))
        right_only = IsNull(col("b", "y"), negated=True)
        cross = Compare(col("a", "x"), "=", col("b", "y"))
        plan = Filter(
            source=join,
            expr=And([left_only, right_only, cross]),
            pids=("L", "R", "X"),
        )
        result = rewrite(plan)
        dest = result.destinations()

        filters = collect_filters(result.plan)
        scan_filters = {
            f.source.alias: f for f in filters if isinstance(f.source, Scan)
        }
        self.assertIn("a", scan_filters)
        self.assertIn("b", scan_filters)
        self.assertEqual(scan_filters["a"].pids, ("L",))
        self.assertEqual(scan_filters["b"].pids, ("R",))

        # Cross-side predicate stays above the join.
        above = [f for f in filters if not isinstance(f.source, Scan)]
        self.assertEqual(len(above), 1)
        self.assertEqual(above[0].pids, ("X",))
        cross_event = next(e for e in result.predicate_events if e.pid == "X")
        self.assertEqual(cross_event.action, "retained")
        self.assertIn("both sides", cross_event.reason)
        self.assertEqual(dest["X"], above[0].node_id)

    def test_left_only_predicate_pushes_through_outer_join(self) -> None:
        t = build_tables()
        a = Scan(alias="a", table=t["A"], node_id="scan_a")
        b = Scan(alias="b", table=t["B"], node_id="scan_b")
        loj = LeftOuterJoin(
            left=a, right=b,
            on=Compare(col("a", "k"), "=", col("b", "k")),
            node_id="loj",
        )
        plan = Filter(
            source=loj,
            expr=And(
                [
                    Compare(col("a", "x"), "<", Literal(100)),
                    IsNull(col("b", "y")),  # keeps the join outer
                ]
            ),
            pids=("left", "right"),
        )
        result = rewrite(plan)
        self.assertEqual(result.conversion_events, [])
        filters = collect_filters(result.plan)
        scan_filters = {
            f.source.alias: f for f in filters if isinstance(f.source, Scan)
        }
        self.assertEqual(scan_filters["a"].pids, ("left",))
        # The right-side IS NULL stays above the outer join.
        above = [f for f in filters if not isinstance(f.source, Scan)]
        self.assertEqual(above[0].pids, ("right",))

    def test_on_conditions_are_never_rewritten(self) -> None:
        t = build_tables()
        a = Scan(alias="a", table=t["A"], node_id="scan_a")
        b = Scan(alias="b", table=t["B"], node_id="scan_b")
        on_expr = Or(
            [
                Compare(col("a", "k"), "=", col("b", "k")),
                IsNull(col("b", "y")),
            ]
        )
        loj = LeftOuterJoin(left=a, right=b, on=on_expr, node_id="loj")
        plan = Filter(
            source=loj,
            expr=Compare(col("a", "x"), ">", Literal(0)),
            pids=("p1",),
        )
        result = rewrite(plan)
        join = collect_joins(result.plan)[0]
        self.assertEqual(join.on, on_expr)

    def test_stacked_filters_are_merged_and_ids_preserved(self) -> None:
        t = build_tables()
        a = Scan(alias="a", table=t["A"], node_id="scan_a")
        b = Scan(alias="b", table=t["B"], node_id="scan_b")
        join = InnerJoin(
            left=a, right=b,
            on=Compare(col("a", "k"), "=", col("b", "k")),
            node_id="ij",
        )
        inner_filter = Filter(
            source=join,
            expr=Compare(col("a", "x"), ">", Literal(0)),
            pids=("p1",),
        )
        outer_filter = Filter(
            source=inner_filter,
            expr=IsNull(col("b", "y"), negated=True),
            pids=("p2",),
        )
        result = rewrite(outer_filter)
        filters = collect_filters(result.plan)
        # No stacked filters in the output.
        for f in filters:
            self.assertNotIsInstance(f.source, Filter)
        self.assertEqual(set(result.destinations()), {"p1", "p2"})

    def test_rewrite_is_idempotent(self) -> None:
        expr = Or(
            [
                Compare(col("c", "z"), "=", Literal(9)),
                IsNull(col("c", "z")),
            ]
        )
        plan = build_three_scan_plan("loj", expr, ("p1",))
        first = rewrite(plan)
        second = rewrite(first.plan)
        self.assertEqual(plan_to_text(first.plan), plan_to_text(second.plan))
        # Second run has no synthetic movement: all conjuncts already sit on
        # filters, and a re-rewrite keeps the same tree shape.
        self.assertEqual(
            [type(j).__name__ for j in collect_joins(second.plan)],
            [type(j).__name__ for j in collect_joins(first.plan)],
        )

    def test_constant_predicate_is_retained_conservatively(self) -> None:
        t = build_tables()
        a = Scan(alias="a", table=t["A"], node_id="scan_a")
        join = InnerJoin(
            left=a,
            right=Scan(alias="b", table=t["B"], node_id="scan_b"),
            on=Compare(col("a", "k"), "=", col("b", "k")),
            node_id="ij",
        )
        plan = Filter(
            source=join, expr=Literal(True), pids=("const",)
        )
        result = rewrite(plan)
        event = result.predicate_events[0]
        self.assertEqual(event.action, "retained")
        self.assertIn("constant", event.reason)

    def test_trace_covers_every_input_predicate(self) -> None:
        plan = build_three_scan_plan(
            "loj",
            And(
                [
                    Compare(col("a", "x"), ">", Literal(0)),
                    Compare(col("c", "z"), "=", Literal(9)),
                    IsNull(col("b", "y")),
                ]
            ),
            ("p1", "p2", "p3"),
        )
        result = rewrite(plan)
        # Every input predicate appears at least once in the trace; a
        # predicate can produce several events when it travels multiple hops
        # (p2 triggers a conversion and is then pushed to its base scan).
        traced_pids = {e.pid for e in result.predicate_events}
        self.assertEqual(traced_pids, {"p1", "p2", "p3"})
        for pid in ("p1", "p2", "p3"):
            self.assertIn(pid, result.destinations())
        # Every event carries a non-empty reason.
        self.assertTrue(all(e.reason for e in result.predicate_events))
        # p2 genuinely travels two hops: conversion hop then scan pushdown.
        p2_hops = result.events_for("p2")
        self.assertEqual(len(p2_hops), 2)
        self.assertTrue(
            any(e.conversion_node is not None for e in p2_hops)
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
