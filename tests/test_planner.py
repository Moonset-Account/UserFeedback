"""Planner tests, including an independent brute-force optimality check."""

import itertools
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
    candidate_keys,
    can_reuse,
    execute_plan,
    load_rows,
    measured_partial_sort,
    measured_stable_sort,
    partial_sort_key,
    partition_prefix_match,
    plan_windows,
    verify,
)
from window_planning.planner import INITIAL_KEY
from window_planning.spec import ROWNO_COLUMN


FEE = FeeSchedule(
    length_fee={1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6, 7: 7, 8: 8},
    comparison_brackets={4: 2, 8: 4, 16: 8, 32: 16, 64: 32},
)


def _key(column: str, direction=Direction.ASC, nulls=Nulls.LAST) -> OrderKey:
    return OrderKey(column, direction, nulls)


def brute_force_optimal(rows, specs, fee_schedule):
    """Enumerate every feasible order x every transition choice.

    This deliberately does not reuse the planner's DP: it walks permutations
    of windows, filters them by dependencies, and independently prices every
    legal REUSE / PARTIAL / FULL sequence. Generated columns are precomputed
    on a working copy exactly as the planner does, since logical values are
    path-independent.
    """
    from window_planning.logical import materialise_logical
    from window_planning.validation import validate_specs

    working_rows = [dict(row) for row in rows]
    materialise_logical(working_rows, specs)
    base = frozenset(rows[0]) - {ROWNO_COLUMN} if rows else frozenset()
    deps = validate_specs(specs, base)
    best_fee = None

    for ordering in itertools.permutations(specs):
        done: set[str] = set()
        for spec in ordering:
            if not set(deps[spec.name]) <= done:
                break
            done.add(spec.name)
        else:
            # This ordering is dependency-feasible; enumerate step choices.
            best_fee = _enumerate_choices(
                working_rows, list(ordering), fee_schedule, INITIAL_KEY,
                tuple(range(len(working_rows))), 0, best_fee,
            )
    return best_fee


def _enumerate_choices(rows, remaining, fee_schedule, key, perm, fee, best_fee):
    if not remaining:
        return fee if best_fee is None else min(best_fee, fee)
    spec = remaining[0]
    rest = remaining[1:]

    if can_reuse(key, spec):
        best_fee = _enumerate_choices(
            rows, rest, fee_schedule, key, perm, fee, best_fee
        )
    if partition_prefix_match(key, spec):
        sort_key = partial_sort_key(key, spec)
        measurement = measured_partial_sort(
            rows, perm, tuple(spec.partition_by), sort_key
        )
        best_fee = _enumerate_choices(
            rows, rest, fee_schedule, sort_key, measurement.permutation,
            fee + fee_schedule.price(measurement), best_fee,
        )
    for candidate in candidate_keys(spec):
        measurement = measured_stable_sort(rows, perm, candidate)
        best_fee = _enumerate_choices(
            rows, rest, fee_schedule, candidate, measurement.permutation,
            fee + fee_schedule.price(measurement), best_fee,
        )
    return best_fee


class PlannerOptimalityTest(unittest.TestCase):
    def test_dp_matches_brute_force_on_random_small_instances(self):
        import random

        rng = random.Random(20261124)
        directions = (Direction.ASC, Direction.DESC)
        null_positions = (Nulls.FIRST, Nulls.LAST)
        for trial in range(12):
            n_rows = rng.randint(1, 6)
            raw_rows = [
                {
                    "p": rng.choice(["A", "B"]),
                    "x": rng.choice([None, 1, 2, 3]),
                    "y": rng.choice([None, 10, 20]),
                }
                for _ in range(n_rows)
            ]
            rows = load_rows(raw_rows)
            specs = []
            for i in range(rng.randint(1, 4)):
                column = rng.choice(("x", "y"))
                if rng.random() < 0.5:
                    specs.append(
                        WindowSpec(
                            f"w{i}",
                            ("p",) if rng.random() < 0.7 else (),
                            (_key(column, rng.choice(directions), rng.choice(null_positions)),),
                            TiePolicy.ROW_NUMBER,
                            WindowFunction.ROW_NUMBER,
                        )
                    )
                else:
                    specs.append(
                        WindowSpec(
                            f"w{i}",
                            ("p",) if rng.random() < 0.7 else (),
                            (_key(column, rng.choice(directions), rng.choice(null_positions)),),
                            TiePolicy.GROUP,
                            rng.choice(
                                [
                                    WindowFunction.DENSE_RANK,
                                    WindowFunction.CUMULATIVE_COUNT,
                                ]
                            ),
                        )
                    )
            plan = plan_windows(rows, specs, FEE)
            expected = brute_force_optimal(rows, specs, FEE)
            self.assertEqual(
                plan.total_fee,
                expected,
                msg=f"trial {trial}: DP fee {plan.total_fee} != brute force {expected}",
            )
            result = execute_plan(rows, specs, plan)
            verify(rows, specs, plan, result, FEE)

    def test_dp_matches_brute_force_with_generated_column_dependency(self):
        rows = load_rows(
            [
                {"p": "A", "x": 2},
                {"p": "A", "x": 1},
                {"p": "B", "x": 1},
                {"p": "B", "x": 3},
            ]
        )
        w1 = WindowSpec(
            "w1", ("p",), (_key("x"),), TiePolicy.ROW_NUMBER, WindowFunction.ROW_NUMBER
        )
        w2 = WindowSpec(
            "w2",
            ("p",),
            (_key("w1"),),
            TiePolicy.GROUP,
            WindowFunction.DENSE_RANK,
        )
        plan = plan_windows(rows, [w1, w2], FEE)
        self.assertEqual(plan.execution_order, ("w1", "w2"))
        self.assertEqual(plan.total_fee, brute_force_optimal(rows, [w1, w2], FEE))


class PlannerChoiceTest(unittest.TestCase):
    def _two_windows_same_logical_key(self):
        rows = load_rows(
            [
                {"dept": "A", "salary": 10},
                {"dept": "A", "salary": 10},
                {"dept": "A", "salary": 20},
                {"dept": "B", "salary": 5},
                {"dept": "B", "salary": 5},
            ]
        )
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
        return rows, rn, dr

    def test_two_windows_with_different_tie_logic_share_one_physical_order(self):
        rows, rn, dr = self._two_windows_same_logical_key()
        plan = plan_windows(rows, [rn, dr], FEE)
        kinds = {step.window: step.kind for step in plan.steps}
        self.assertEqual(kinds["rn"], StepKind.FULL)
        self.assertEqual(kinds["dr"], StepKind.REUSE)
        self.assertEqual(plan.steps[1].fee, 0)
        # Both windows really do evaluate against the same physical order.
        self.assertEqual(
            plan.steps[1].physical_key, plan.steps[0].physical_key
        )
        result = execute_plan(rows, [rn, dr], plan)
        report = verify(rows, [rn, dr], plan, result, FEE, shared_order_example=True)
        self.assertTrue(report.shared_order_example)
        # The logical tie semantics still differ on the duplicated salary.
        self.assertEqual([row["rn"] for row in result.rows[:2]], [1, 2])
        self.assertEqual([row["dr"] for row in result.rows[:2]], [1, 1])

    def test_direction_change_forbids_reuse_and_prefers_partial_sort(self):
        rows = load_rows(
            [
                {"dept": "A", "salary": 1},
                {"dept": "A", "salary": 2},
                {"dept": "B", "salary": 1},
                {"dept": "B", "salary": 2},
            ]
        )
        asc = WindowSpec(
            "asc_win",
            ("dept",),
            (_key("salary"),),
            TiePolicy.ROW_NUMBER,
            WindowFunction.ROW_NUMBER,
        )
        desc = WindowSpec(
            "desc_win",
            ("dept",),
            (_key("salary", Direction.DESC, Nulls.FIRST),),
            TiePolicy.ROW_NUMBER,
            WindowFunction.ROW_NUMBER,
        )
        plan = plan_windows(rows, [asc, desc], FEE)
        second = next(step for step in plan.steps if step.window == "desc_win")
        self.assertEqual(second.kind, StepKind.PARTIAL)
        self.assertIn("partition prefix", second.reuse_basis)
        result = execute_plan(rows, [asc, desc], plan)
        verify(rows, [asc, desc], plan, result, FEE)

    def test_empty_partition_and_order_window_reuses_initial_order_for_free(self):
        rows = load_rows([{"v": 3}, {"v": 1}, {"v": 2}])
        trivial = WindowSpec(
            "trivial",
            (),
            (),
            TiePolicy.ROW_NUMBER,
            WindowFunction.ROW_NUMBER,
        )
        plan = plan_windows(rows, [trivial], FEE)
        self.assertEqual(plan.steps[0].kind, StepKind.REUSE)
        self.assertEqual(plan.total_fee, 0)

    def test_result_rows_are_restored_to_original_row_numbers(self):
        rows, rn, dr = self._two_windows_same_logical_key()
        plan = plan_windows(rows, [rn, dr], FEE)
        result = execute_plan(rows, [rn, dr], plan)
        self.assertEqual(
            [row[ROWNO_COLUMN] for row in result.rows], list(range(len(rows)))
        )

    def test_eight_window_limit_is_plannable(self):
        rows = load_rows([{"p": "A", "x": 1}, {"p": "B", "x": 2}])
        specs = [
            WindowSpec(
                f"w{i}",
                ("p",),
                (_key("x"),),
                TiePolicy.GROUP,
                WindowFunction.DENSE_RANK,
            )
            for i in range(8)
        ]
        plan = plan_windows(rows, specs, FEE)
        self.assertEqual(len(plan.steps), 8)
        # One full sort, seven free reuses.
        self.assertEqual(sum(step.fee for step in plan.steps[1:]), 0)
        result = execute_plan(rows, specs, plan)
        verify(rows, specs, plan, result, FEE)


if __name__ == "__main__":
    unittest.main()
