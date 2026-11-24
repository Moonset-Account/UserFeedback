"""Runnable demonstration of the window physical planning library.

Run from the workspace root with::

    python3 -m window_planning.demo

The scenario is synthetic local data only. It deliberately includes:

* two windows with different logical tie semantics sharing one physical order;
* a window keyed by another window's generated column (a real dependency);
* a window reusing an already contiguous partition prefix via an in-group
  stable sort (PARTIAL) instead of a full sort;
* direction / NULL-position differences that forbid order reuse.
"""

from __future__ import annotations

from .cost import FeeSchedule
from .executor import execute_plan
from .planner import StepKind, plan_windows
from .spec import (
    Direction,
    Nulls,
    OrderKey,
    TiePolicy,
    WindowFunction,
    WindowSpec,
)
from .validation import load_rows
from .verification import verify

# Small explicit fee tables covering every value the demo can observe.
LENGTH_FEE = {1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6, 7: 7, 8: 8}
COMPARISON_BRACKETS = {4: 2, 8: 4, 16: 8, 32: 16, 64: 32}


def build_demo_rows() -> list[dict]:
    raw_rows = [
        {"dept": "A", "salary": 10, "bonus": 1},
        {"dept": "A", "salary": 10, "bonus": 2},
        {"dept": "A", "salary": 20, "bonus": None},
        {"dept": "B", "salary": 5, "bonus": 9},
        {"dept": "B", "salary": 5, "bonus": 4},
        {"dept": "B", "salary": None, "bonus": 7},
    ]
    return load_rows(raw_rows)


def build_demo_specs() -> list[WindowSpec]:
    salary_asc = OrderKey("salary", Direction.ASC, Nulls.LAST)
    return [
        # (1) Row number per department by salary: builds the shared order.
        WindowSpec(
            name="row_num",
            partition_by=("dept",),
            order_by=(salary_asc,),
            tie_policy=TiePolicy.ROW_NUMBER,
            function=WindowFunction.ROW_NUMBER,
        ),
        # (2) Dense rank with identical keys: different logical tie semantics,
        #     yet the very same physical order is reusable for free.
        WindowSpec(
            name="dense_rank_salary",
            partition_by=("dept",),
            order_by=(salary_asc,),
            tie_policy=TiePolicy.GROUP,
            function=WindowFunction.DENSE_RANK,
        ),
        # (3) Cumulative count on the same logical key.
        WindowSpec(
            name="cum_count_salary",
            partition_by=("dept",),
            order_by=(salary_asc,),
            tie_policy=TiePolicy.GROUP,
            function=WindowFunction.CUMULATIVE_COUNT,
        ),
        # (4) Depends on the generated row_num column: dependency edge.
        WindowSpec(
            name="rank_by_row_num",
            partition_by=("dept",),
            order_by=(
                OrderKey("row_num", Direction.ASC, Nulls.LAST),
            ),
            tie_policy=TiePolicy.GROUP,
            function=WindowFunction.DENSE_RANK,
        ),
        # (5) Same partition prefix, different ordering key: partial stable
        #     sort inside the already contiguous department groups.
        WindowSpec(
            name="row_num_by_bonus",
            partition_by=("dept",),
            order_by=(
                OrderKey("bonus", Direction.ASC, Nulls.FIRST),
            ),
            tie_policy=TiePolicy.ROW_NUMBER,
            function=WindowFunction.ROW_NUMBER,
        ),
        # (6) DESC + NULLS FIRST on salary: cannot reuse the ASC order.
        WindowSpec(
            name="row_num_salary_desc",
            partition_by=("dept",),
            order_by=(
                OrderKey("salary", Direction.DESC, Nulls.FIRST),
            ),
            tie_policy=TiePolicy.ROW_NUMBER,
            function=WindowFunction.ROW_NUMBER,
        ),
    ]


def run_demo() -> None:
    rows = build_demo_rows()
    specs = build_demo_specs()
    fee_schedule = FeeSchedule(
        length_fee=dict(LENGTH_FEE),
        comparison_brackets=dict(COMPARISON_BRACKETS),
    )

    print("=" * 72)
    print("WINDOW SPECIFICATIONS")
    print("=" * 72)
    for spec in specs:
        print(" -", spec.describe())
    print()
    print("dependency edges:")
    plan = plan_windows(rows, specs, fee_schedule)
    for name, deps in plan.dependencies.items():
        print(f"  {name} <- {deps if deps else '(none)'}")

    print()
    print("=" * 72)
    print("MINIMUM-FEE PHYSICAL PLAN")
    print("=" * 72)
    print(plan.describe())

    result = execute_plan(rows, specs, plan)
    report = verify(
        rows,
        specs,
        plan,
        result,
        fee_schedule,
        shared_order_example=True,
    )

    print()
    print("=" * 72)
    print("OUTPUT COLUMNS IN ORIGINAL ROW ORDER")
    print("=" * 72)
    header = ("row", "dept", "salary", "bonus") + tuple(s.name for s in specs)
    print(" | ".join(f"{column:>18}" for column in header))
    for row in result.rows:
        values = [
            row["__rowno__"],
            row["dept"],
            str(row["salary"]),
            str(row["bonus"]),
        ] + [row[spec.name] for spec in specs]
        print(" | ".join(f"{str(value):>18}" for value in values))

    print()
    print("=" * 72)
    print("INDEPENDENT VERIFICATION")
    print("=" * 72)
    print(report.describe())

    shared = [
        step
        for step in plan.steps
        if step.kind is StepKind.REUSE
    ]
    partial = [
        step
        for step in plan.steps
        if step.kind is StepKind.PARTIAL
    ]
    print()
    print(
        f"reuse steps: {[step.window for step in shared]}; "
        f"partial-sort steps: {[step.window for step in partial]}"
    )
    print("all logical values and every measured fee were independently verified")


if __name__ == "__main__":
    run_demo()
