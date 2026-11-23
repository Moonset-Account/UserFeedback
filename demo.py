#!/usr/bin/env python3
"""End-to-end demonstration on the task's two mandatory scenarios.

The script builds the *same* two-level left outer join twice:

1. a WHERE clause containing IS NULL + OR that must keep both joins outer;
2. a similar plan whose top filter is null-rejecting, legally converting the
   top left outer join into an inner join (with continued pushdown).

For each scenario it prints the original plan, the rewritten plan, the
per-predicate trace and the conversion record, and then verifies bag
equivalence between original and rewritten plans by actually evaluating
both. Nothing here uses precomputed output -- every result is produced by
the package at runtime.

Run from the workspace root::

    python3 -I demo.py
"""

from __future__ import annotations

import os
import sys

# Make the package importable when launched under `python -I`, which does
# not put the script's directory on sys.path automatically.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from predrewrite.expressions import And, Column, Compare, IsNull, Literal, Or
from predrewrite.plans import (
    Filter,
    LeftOuterJoin,
    Scan,
    Table,
    canonical_bag,
    evaluate_plan,
    plan_to_text,
)
from predrewrite.rewrite import rewrite


def col(alias: str, name: str) -> Column:
    return Column(alias, name)


def build_plan(where, pids) -> Filter:
    # Small tables deliberately include duplicate rows and NULL values.
    t_a = Table(
        "A", ("k", "x"),
        (
            {"k": 1, "x": 7},
            {"k": 1, "x": 7},   # duplicate left row
            {"k": 2, "x": None},
        ),
    )
    t_b = Table(
        "B", ("k", "y"),
        ({"k": 1, "y": 5}, {"k": 3, "y": None}),
    )
    t_c = Table(
        "C", ("k", "z"),
        ({"k": 1, "z": 9}, {"k": 4, "z": None}),
    )
    a = Scan(alias="a", table=t_a, node_id="scan_a")
    b = Scan(alias="b", table=t_b, node_id="scan_b")
    c = Scan(alias="c", table=t_c, node_id="scan_c")
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


def show(title: str, plan: Filter) -> None:
    print("=" * 78)
    print(title)
    print("=" * 78)
    print("-- original plan --")
    print(plan_to_text(plan))

    result = rewrite(plan)
    print("\n-- rewritten plan --")
    print(plan_to_text(result.plan))

    print("\n-- predicate trace --")
    for event in result.predicate_events:
        conv = (
            f" [conversion {event.conversion_node}]"
            if event.conversion_node else ""
        )
        print(
            f"  {event.pid:6s} {event.action:8s} -> {event.destination:16s}"
            f"{conv}\n         {event.text}\n         why: {event.reason}"
        )

    print("\n-- outer-to-inner conversions --")
    if result.conversion_events:
        for conv in result.conversion_events:
            print(
                f"  {conv.outer_node_id} -> {conv.inner_node_id} "
                f"triggered by {', '.join(conv.triggered_by)}\n         "
                f"{conv.justification}"
            )
    else:
        print("  none -- every outer join is preserved")

    before = canonical_bag(evaluate_plan(plan))
    after = canonical_bag(evaluate_plan(result.plan))
    print(
        f"\n-- runtime bag equivalence check --\n"
        f"  original output rows: {sum(before.values())}\n"
        f"  rewritten output rows: {sum(after.values())}\n"
        f"  identical multisets: {before == after}"
    )
    assert before == after, "rewrite changed the query result"
    print()


def main() -> None:
    # Scenario 1: IS NULL + OR. The OR has an explicit IS NULL branch so it
    # is TRUE on a c-padded row; the b-side IS NULL is TRUE on b-padded
    # rows. No conversion is provable, so both joins stay outer.
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
    show("Scenario 1: two-level LOJ with IS NULL and OR (must stay outer)",
         build_plan(retain_where, ("p_or", "p_bnull")))

    # Scenario 2: same shape, but the c-side OR has no IS NULL branch
    # (UNKNOWN on padding) and the b-side predicate is an IS NOT NULL. The
    # top join converts to inner; pushdown then continues down to scans.
    convert_where = And(
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
    show("Scenario 2: similar plan that legally converts an LOJ to inner",
         build_plan(convert_where, ("p_or", "p_bnotnull")))


if __name__ == "__main__":
    main()
