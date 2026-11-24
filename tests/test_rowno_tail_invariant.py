"""Invariant: the physical row-number tail never splits a logical peer group."""

import unittest

from window_planning import (
    Direction,
    FeeSchedule,
    Nulls,
    OrderKey,
    TiePolicy,
    WindowFunction,
    WindowSpec,
    execute_plan,
    load_rows,
    plan_windows,
)
from window_planning.physical import KeyRole
from window_planning.planner import StepKind


FEE = FeeSchedule(
    length_fee={1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6, 7: 7, 8: 8},
    comparison_brackets={4: 2, 8: 4, 16: 8, 32: 16, 64: 32},
)


class RowNoTailInvariantTest(unittest.TestCase):
    def test_rowno_tail_is_always_last_when_present(self):
        rows = load_rows(
            [
                {"dept": "A", "salary": 10},
                {"dept": "A", "salary": 10},
                {"dept": "A", "salary": 20},
                {"dept": "B", "salary": 10},
            ]
        )
        specs = [
            WindowSpec(
                "rn",
                ("dept",),
                (OrderKey("salary", Direction.ASC, Nulls.LAST),),
                TiePolicy.ROW_NUMBER,
                WindowFunction.ROW_NUMBER,
            ),
            WindowSpec(
                "dr",
                ("dept",),
                (OrderKey("salary", Direction.ASC, Nulls.LAST),),
                TiePolicy.GROUP,
                WindowFunction.DENSE_RANK,
            ),
        ]
        plan = plan_windows(rows, specs, FEE)
        for step in plan.steps:
            tails = [
                part
                for part in step.physical_key.parts
                if part.role is KeyRole.ROWNO_TAIL
            ]
            self.assertLessEqual(len(tails), 1)
            if tails:
                self.assertIs(step.physical_key.parts[-1], tails[0])

    def test_peer_rows_share_dense_rank_and_cumulative_count_despite_tail(self):
        rows = load_rows(
            [
                {"dept": "A", "salary": 10},
                {"dept": "A", "salary": 10},
                {"dept": "A", "salary": 10},
            ]
        )
        rn = WindowSpec(
            "rn",
            ("dept",),
            (OrderKey("salary", Direction.ASC, Nulls.LAST),),
            TiePolicy.ROW_NUMBER,
            WindowFunction.ROW_NUMBER,
        )
        dr = WindowSpec(
            "dr",
            ("dept",),
            (OrderKey("salary", Direction.ASC, Nulls.LAST),),
            TiePolicy.GROUP,
            WindowFunction.DENSE_RANK,
        )
        cc = WindowSpec(
            "cc",
            ("dept",),
            (OrderKey("salary", Direction.ASC, Nulls.LAST),),
            TiePolicy.GROUP,
            WindowFunction.CUMULATIVE_COUNT,
        )
        plan = plan_windows(rows, [rn, dr, cc], FEE)
        result = execute_plan(rows, [rn, dr, cc], plan)
        # The tail separates the rows physically (1, 2, 3) ...
        self.assertEqual([row["rn"] for row in result.rows], [1, 2, 3])
        # ... but the logical group stays intact for GROUP windows.
        self.assertEqual([row["dr"] for row in result.rows], [1, 1, 1])
        self.assertEqual([row["cc"] for row in result.rows], [3, 3, 3])
        # The GROUP windows must have reused the ROW_NUMBER order for free.
        self.assertTrue(
            all(
                step.kind is StepKind.REUSE
                for step in plan.steps
                if step.window in {"dr", "cc"}
            )
        )


if __name__ == "__main__":
    unittest.main()
