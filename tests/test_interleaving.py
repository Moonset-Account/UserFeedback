"""Interleaving and stability scenarios.

These tests treat row-arrival interleavings and physical-order interleavings
as acceptance conditions: the minimum fee and every logical value must remain
correct regardless of the order in which rows are presented, and partial
sorts must preserve interleaved partition-group positions.
"""

import unittest

from window_planning import (
    Direction,
    FeeSchedule,
    Nulls,
    OrderKey,
    StepKind,
    TiePolicy,
    WindowFunction,
    WindowSpec,
    execute_plan,
    load_rows,
    plan_windows,
    verify,
)


FEE = FeeSchedule(
    length_fee={1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6, 7: 7, 8: 8},
    comparison_brackets={4: 2, 8: 4, 16: 8, 32: 16, 64: 32},
)


def _key(column: str, direction=Direction.ASC, nulls=Nulls.LAST) -> OrderKey:
    return OrderKey(column, direction, nulls)


ROWS_BY_DEPT = [
    {"dept": "A", "salary": 30, "bonus": 3},
    {"dept": "A", "salary": 10, "bonus": 1},
    {"dept": "A", "salary": 20, "bonus": 2},
    {"dept": "B", "salary": 30, "bonus": 6},
    {"dept": "B", "salary": 10, "bonus": 4},
    {"dept": "B", "salary": 20, "bonus": 5},
]

# Three different physical interleavings of the same logical rows.
INTERLEAVINGS = {
    "grouped": ROWS_BY_DEPT,
    "alternating": [
        ROWS_BY_DEPT[0],
        ROWS_BY_DEPT[3],
        ROWS_BY_DEPT[1],
        ROWS_BY_DEPT[4],
        ROWS_BY_DEPT[2],
        ROWS_BY_DEPT[5],
    ],
    "reversed": list(reversed(ROWS_BY_DEPT)),
}


def _specs():
    rn = WindowSpec(
        "rn",
        ("dept",),
        (_key("salary"),),
        TiePolicy.ROW_NUMBER,
        WindowFunction.ROW_NUMBER,
    )
    dr = WindowSpec(
        "dr",
        ("dept",),
        (_key("salary"),),
        TiePolicy.GROUP,
        WindowFunction.DENSE_RANK,
    )
    cc = WindowSpec(
        "cc",
        ("dept",),
        (_key("bonus"),),
        TiePolicy.GROUP,
        WindowFunction.CUMULATIVE_COUNT,
    )
    return [rn, dr, cc]


class InterleavingTest(unittest.TestCase):
    def test_logical_values_are_invariant_under_row_interleavings(self):
        keyed_outputs = {}
        for label, raw in INTERLEAVINGS.items():
            rows = load_rows(raw)
            plan = plan_windows(rows, _specs(), FEE)
            result = execute_plan(rows, _specs(), plan)
            verify(rows, _specs(), plan, result, FEE)
            # Re-key outputs by the immutable business identity (dept, salary,
            # bonus) so arrival order cannot mask a value mismatch.
            keyed_outputs[label] = {
                (row["dept"], row["salary"], row["bonus"]): (
                    row["rn"],
                    row["dr"],
                    row["cc"],
                )
                for row in result.rows
            }
        reference = keyed_outputs["grouped"]
        for label, values in keyed_outputs.items():
            self.assertEqual(values, reference, msg=f"interleaving {label} changed values")

    def test_partial_sort_keeps_interleaved_group_positions_contiguous(self):
        rows = load_rows(INTERLEAVINGS["alternating"])
        specs = _specs()
        plan = plan_windows(rows, specs, FEE)
        # At least one partial sort must occur after the first window makes
        # 'dept' contiguous; groups may not cross each other afterwards.
        partial_steps = [
            step for step in plan.steps if step.kind is StepKind.PARTIAL
        ]
        self.assertTrue(partial_steps)
        result = execute_plan(rows, specs, plan)
        # Replay the final physical order from the last audit entry and
        # confirm each department forms one uninterrupted block there. The
        # *returned* rows are separately restored to original row numbers.
        last_perm = result.executed_steps[-1].measurement.permutation
        physical_depts = [rows[i]["dept"] for i in last_perm]
        blocks: list[str] = []
        current = None
        for dept in physical_depts:
            if dept != current:
                blocks.append(dept)
                current = dept
        self.assertEqual(sorted(blocks), ["A", "B"])
        self.assertEqual(len(blocks), len(set(blocks)))
        # Result rows themselves stay in original order.
        from window_planning.spec import ROWNO_COLUMN

        self.assertEqual(
            [row[ROWNO_COLUMN] for row in result.rows], list(range(len(rows)))
        )

    def test_planner_fee_adapts_to_interleaving_but_plan_stays_optimal(self):
        from tests.test_planner import brute_force_optimal

        fees = {}
        for label, raw in INTERLEAVINGS.items():
            rows = load_rows(raw)
            specs = _specs()
            plan = plan_windows(rows, specs, FEE)
            self.assertEqual(plan.total_fee, brute_force_optimal(rows, specs, FEE))
            fees[label] = plan.total_fee
        # The grouped arrival can only be cheaper than (or equal to) a highly
        # interleaved one, because fewer inversions need repair.
        self.assertLessEqual(fees["grouped"], fees["alternating"])
        self.assertLessEqual(fees["grouped"], fees["reversed"])

    def test_null_interleavings_keep_peer_groups_intact(self):
        raw = [
            {"p": "A", "v": None},
            {"p": "B", "v": 1},
            {"p": "A", "v": 1},
            {"p": "B", "v": None},
        ]
        spec = WindowSpec(
            "dr",
            ("p",),
            (_key("v", Direction.ASC, Nulls.FIRST),),
            TiePolicy.GROUP,
            WindowFunction.DENSE_RANK,
        )
        rows = load_rows(raw)
        plan = plan_windows(rows, [spec], FEE)
        result = execute_plan(rows, [spec], plan)
        verify(rows, [spec], plan, result, FEE)
        by_identity = {
            (row["p"], row["v"]): row["dr"] for row in result.rows
        }
        # NULL and 1 are two distinct peers inside each partition.
        self.assertEqual(by_identity[("A", None)], 1)
        self.assertEqual(by_identity[("A", 1)], 2)
        self.assertEqual(by_identity[("B", None)], 1)
        self.assertEqual(by_identity[("B", 1)], 2)


if __name__ == "__main__":
    unittest.main()
