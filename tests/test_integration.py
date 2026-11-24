"""End-to-end integration and failure-injection tests."""

import unittest

from window_planning import (
    Direction,
    FeeSchedule,
    Nulls,
    OrderKey,
    StepKind,
    TiePolicy,
    VerificationError,
    WindowFunction,
    WindowSpec,
    execute_plan,
    load_rows,
    plan_windows,
    verify,
)
from window_planning.planner import PlanStep
from window_planning.physical import PhysKey
from window_planning.spec import ROWNO_COLUMN


FEE = FeeSchedule(
    length_fee={1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6, 7: 7, 8: 8},
    comparison_brackets={4: 2, 8: 4, 16: 8, 32: 16, 64: 32},
)


def _key(column: str, direction=Direction.ASC, nulls=Nulls.LAST) -> OrderKey:
    return OrderKey(column, direction, nulls)


def _interlocking_specs():
    # w3 depends on w1; w1 and w2 share keys; w4 re-sorts on another column.
    w1 = WindowSpec(
        "w1",
        ("dept",),
        (_key("salary"),),
        TiePolicy.ROW_NUMBER,
        WindowFunction.ROW_NUMBER,
    )
    w2 = WindowSpec(
        "w2",
        ("dept",),
        (_key("salary"),),
        TiePolicy.GROUP,
        WindowFunction.CUMULATIVE_COUNT,
    )
    w3 = WindowSpec(
        "w3",
        ("dept",),
        (_key("w1"),),
        TiePolicy.GROUP,
        WindowFunction.DENSE_RANK,
    )
    w4 = WindowSpec(
        "w4",
        ("dept",),
        (_key("bonus", Direction.DESC, Nulls.FIRST),),
        TiePolicy.ROW_NUMBER,
        WindowFunction.ROW_NUMBER,
    )
    return [w1, w2, w3, w4]


class EndToEndTest(unittest.TestCase):
    def setUp(self):
        self.rows = load_rows(
            [
                {"dept": "A", "salary": 10, "bonus": 1},
                {"dept": "A", "salary": 10, "bonus": 2},
                {"dept": "A", "salary": 20, "bonus": None},
                {"dept": "B", "salary": 5, "bonus": 9},
                {"dept": "B", "salary": 5, "bonus": 4},
                {"dept": "B", "salary": None, "bonus": 7},
            ]
        )
        self.specs = _interlocking_specs()

    def test_full_pipeline_plans_executes_and_verifies(self):
        plan = plan_windows(self.rows, self.specs, FEE)
        result = execute_plan(self.rows, self.specs, plan)
        report = verify(self.rows, self.specs, plan, result, FEE)

        # Dependency order: w1 before w3.
        self.assertLess(
            plan.execution_order.index("w1"), plan.execution_order.index("w3")
        )
        # Every window appears exactly once.
        self.assertEqual(sorted(plan.execution_order), sorted(s.name for s in self.specs))
        # Fees reported by planner, executor and independent audit all agree.
        self.assertEqual(plan.total_fee, result.total_fee)
        self.assertEqual(report.total_fee, plan.total_fee)
        for audit in report.fee_checks:
            self.assertEqual(audit.measured_fee, audit.planner_fee)
            self.assertEqual(audit.measured_fee, audit.executor_fee)
        # Output stays in original row order.
        self.assertEqual(
            [row[ROWNO_COLUMN] for row in result.rows], list(range(len(self.rows)))
        )

    def test_dependent_window_sees_generated_column_values(self):
        plan = plan_windows(self.rows, self.specs, FEE)
        result = execute_plan(self.rows, self.specs, plan)
        # w3 dense-ranks the unique w1 row numbers, so ranks are 1..3 per dept.
        by_dept = {}
        for row in result.rows:
            by_dept.setdefault(row["dept"], []).append(row["w3"])
        self.assertEqual(sorted(by_dept["A"]), [1, 2, 3])
        self.assertEqual(sorted(by_dept["B"]), [1, 2, 3])

    def test_verification_detects_tampered_logical_output(self):
        plan = plan_windows(self.rows, self.specs, FEE)
        result = execute_plan(self.rows, self.specs, plan)
        result.rows[0]["w2"] = 999
        with self.assertRaises(VerificationError):
            verify(self.rows, self.specs, plan, result, FEE)

    def test_verification_detects_tampered_plan_fee(self):
        plan = plan_windows(self.rows, self.specs, FEE)
        result = execute_plan(self.rows, self.specs, plan)
        steps = list(plan.steps)
        tampered_step = steps[0]
        steps[0] = PlanStep(
            window=tampered_step.window,
            kind=tampered_step.kind,
            physical_key=tampered_step.physical_key,
            reuse_basis=tampered_step.reuse_basis,
            comparisons=tampered_step.comparisons,
            group_sizes=tampered_step.group_sizes,
            fee=tampered_step.fee + 1,
        )
        plan.steps = steps
        with self.assertRaises(VerificationError):
            verify(self.rows, self.specs, plan, result, FEE)

    def test_verification_detects_tampered_sort_key(self):
        plan = plan_windows(self.rows, self.specs, FEE)
        result = execute_plan(self.rows, self.specs, plan)
        steps = list(plan.steps)
        original = steps[0]
        bogus_key = PhysKey(tuple(part for part in original.physical_key.parts))
        # Replace the ordering component's column by swapping in another spec.
        from window_planning.physical import order_part

        broken_parts = list(bogus_key.parts)
        broken_parts[1] = order_part(
            OrderKey("bonus", Direction.ASC, Nulls.FIRST)
        )
        steps[0] = PlanStep(
            window=original.window,
            kind=original.kind,
            physical_key=PhysKey(tuple(broken_parts)),
            reuse_basis=original.reuse_basis,
            comparisons=original.comparisons,
            group_sizes=original.group_sizes,
            fee=original.fee,
        )
        plan.steps = steps
        with self.assertRaises(VerificationError):
            verify(self.rows, self.specs, plan, result, FEE)

    def test_executor_rejects_plan_for_other_window_set(self):
        plan = plan_windows(self.rows, self.specs, FEE)
        other = self.specs[:-1]
        with self.assertRaises(Exception):
            execute_plan(self.rows, other, plan)

    def test_plan_contains_real_measurements_not_placeholder_zeros(self):
        plan = plan_windows(self.rows, self.specs, FEE)
        sorting_steps = [
            step for step in plan.steps if step.kind is not StepKind.REUSE
        ]
        self.assertTrue(sorting_steps)
        for step in sorting_steps:
            self.assertGreaterEqual(step.comparisons, 0)
            self.assertTrue(step.group_sizes)
            self.assertGreater(step.fee, 0)
            self.assertTrue(step.physical_key.describe())


class EmptyTableIntegrationTest(unittest.TestCase):
    def test_pipeline_on_empty_table(self):
        rows = load_rows([])
        spec = WindowSpec(
            "w",
            (),
            (_key("v"),),
            TiePolicy.ROW_NUMBER,
            WindowFunction.ROW_NUMBER,
        )
        plan = plan_windows(rows, [spec], FEE)
        result = execute_plan(rows, [spec], plan)
        report = verify(rows, [spec], plan, result, FEE)
        self.assertEqual(result.rows, [])
        self.assertEqual(report.total_fee, 0)
        self.assertEqual(plan.steps[0].kind, StepKind.REUSE)


if __name__ == "__main__":
    unittest.main()
