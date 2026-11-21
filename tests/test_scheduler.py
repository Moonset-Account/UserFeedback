"""Unit tests for the change-window scheduler.

Run from the workspace root::

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import itertools
import json
import os
import subprocess
import sys
import tempfile
import unittest

# Make the package importable when tests are run from the workspace root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from change_scheduler import (  # noqa: E402
    ChangePlan,
    Interval,
    SchedulerError,
    Step,
    plan_change,
    verify_plan,
)

WORKSPACE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def step(dur, win, rdur, duty, name=""):
    return Step(
        duration=dur,
        window=Interval(*win),
        rollback_duration=rdur,
        duty_window=Interval(*duty),
        name=name,
    )


def brute_force_latest_start(steps, deadline):
    """Independent exhaustive search over small integer time domains.

    Returns the latest feasible start minute, or ``None`` if infeasible.
    Enumerates end times ``e_i`` for every step within its window and
    ordering, then checks each failure scenario's contiguous reverse
    rollback against the duty windows.
    """
    n = len(steps)
    if n == 0:
        return deadline

    def failure_ok(k, ends):
        e = ends[k - 1]
        cursor = e
        for j in range(k, 0, -1):
            st = steps[j - 1]
            if not st.duty_window.contains_span(cursor, cursor + st.rollback_duration):
                return False
            cursor += st.rollback_duration
        return True

    # Candidate end values per step (only those that fit window and deadline).
    candidates = []
    for i, st in enumerate(steps):
        vals = []
        for e in range(st.window.start + st.duration, st.window.end + 1):
            if i == n - 1 and e > deadline:
                continue
            vals.append(e)
        candidates.append(vals)

    best = None
    for ends in itertools.product(*candidates):
        ok = True
        for i in range(n - 1):
            if ends[i] + steps[i + 1].duration > ends[i + 1]:
                ok = False
                break
        if not ok:
            continue
        for k in range(1, n + 1):
            if not failure_ok(k, ends):
                ok = False
                break
        if not ok:
            continue
        start = ends[0] - steps[0].duration
        if best is None or start > best:
            best = start
    return best


class TestBasicFeasible(unittest.TestCase):
    def test_single_step_forward_and_rollback(self):
        steps = [step(10, (0, 100), 5, (0, 200), "only")]
        plan = plan_change(steps, deadline=100)
        self.assertTrue(plan.feasible)
        self.assertEqual(plan.start_time, 90)  # latest: end == deadline
        self.assertEqual(verify_plan(steps, 100, plan), [])
        self.assertEqual(plan.forward[0].start, 90)
        self.assertEqual(plan.forward[0].end, 100)
        failure = plan.failures[0]
        self.assertEqual(failure.failure_time, 100)
        self.assertEqual(failure.rollbacks[0].start, 100)
        self.assertEqual(failure.rollbacks[0].end, 105)

    def test_deadline_can_bind_before_window(self):
        # Window allows until 500, but deadline is 50.
        steps = [step(10, (0, 500), 5, (0, 100))]
        plan = plan_change(steps, deadline=50)
        self.assertTrue(plan.feasible)
        self.assertEqual(plan.start_time, 40)
        self.assertEqual(plan.forward[0].end, 50)

    def test_window_can_bind_before_deadline(self):
        steps = [step(10, (0, 45), 5, (0, 100))]
        plan = plan_change(steps, deadline=1000)
        self.assertTrue(plan.feasible)
        self.assertEqual(plan.forward[0].end, 45)
        self.assertEqual(plan.start_time, 35)

    def test_waiting_between_steps_allowed(self):
        # Both windows start late but disjoint; scheduler must insert a wait.
        steps = [
            step(5, (0, 30), 2, (0, 100)),
            step(5, (60, 90), 2, (0, 100)),
        ]
        plan = plan_change(steps, deadline=200)
        self.assertTrue(plan.feasible)
        self.assertEqual(verify_plan(steps, 200, plan), [])
        self.assertGreaterEqual(plan.forward[1].start, plan.forward[0].end)
        # Latest start maximises step1's end, bounded only by its own window
        # end (30).  Step2 sits as late as its window allows (end 90), which
        # still leaves a wait between the two steps.
        self.assertEqual(plan.forward[1].end, 90)
        self.assertEqual(plan.forward[0].end, 30)
        self.assertEqual(plan.start_time, 25)
        self.assertGreater(plan.forward[1].start, plan.forward[0].end)

    def test_empty_change(self):
        plan = plan_change([], deadline=123)
        self.assertTrue(plan.feasible)
        self.assertEqual(plan.start_time, 123)
        self.assertEqual(plan.forward, ())
        self.assertEqual(plan.failures, ())
        self.assertEqual(verify_plan([], 123, plan), [])


class TestRollbackConstraints(unittest.TestCase):
    def test_failure_at_last_step_reverse_contiguous(self):
        steps = [
            step(10, (0, 100), 3, (0, 200), "a"),
            step(10, (0, 100), 4, (0, 200), "b"),
            step(10, (0, 100), 5, (0, 200), "c"),
        ]
        plan = plan_change(steps, deadline=100)
        self.assertTrue(plan.feasible)
        self.assertEqual(verify_plan(steps, 100, plan), [])
        failure = plan.failures[2]
        self.assertEqual(failure.failed_step, 3)
        self.assertEqual([r.step_index for r in failure.rollbacks], [3, 2, 1])
        cursor = failure.failure_time
        for action in failure.rollbacks:
            self.assertEqual(action.start, cursor)
            cursor = action.end

    def test_every_failure_point_planned(self):
        steps = [
            step(5, (0, 100), 2, (0, 200), "a"),
            step(5, (0, 100), 2, (0, 200), "b"),
            step(5, (0, 100), 2, (0, 200), "c"),
        ]
        plan = plan_change(steps, deadline=100)
        self.assertEqual([f.failed_step for f in plan.failures], [1, 2, 3])
        for i, f in enumerate(plan.failures, start=1):
            self.assertEqual(f.failure_time, plan.forward[i - 1].end)
            self.assertEqual([r.step_index for r in f.rollbacks], list(range(i, 0, -1)))

    def test_rollback_duty_limits_force_early_forward_end(self):
        # Forward window is wide, but duty window closes at 50.  Step duration
        # 10, rollback 5 -> forward end must be <= 45.
        steps = [step(10, (0, 200), 5, (0, 50))]
        plan = plan_change(steps, deadline=200)
        self.assertTrue(plan.feasible)
        self.assertEqual(plan.forward[0].end, 45)
        self.assertEqual(plan.start_time, 35)
        self.assertEqual(plan.failures[0].rollbacks[0].end, 50)

    def test_duty_window_opens_late_forces_late_forward_end(self):
        # Rollback duty opens at 100; rollback 5 -> forward end >= 100.
        steps = [step(10, (0, 200), 5, (100, 150))]
        plan = plan_change(steps, deadline=200)
        self.assertTrue(plan.feasible)
        self.assertEqual(plan.forward[0].end, 145)  # <= duty.end - rollback
        self.assertGreaterEqual(plan.forward[0].end, 100)

    def test_forward_ok_but_first_rollback_misses_duty(self):
        """Explicitly required: forward window fits, but once the first step is
        done its rollback cannot meet its duty window -> not releasable."""
        # Forward window [30, 100) fits a 10-minute step, but the duty window
        # closes at 20 while a 10-minute rollback must fit in it: forward end
        # would have to be <= 10, below the window's earliest possible end 40.
        steps = [step(10, (30, 100), 10, (0, 20), "first")]
        plan = plan_change(steps, deadline=100)
        self.assertFalse(plan.feasible)
        self.assertIsNone(plan.start_time)
        self.assertIn("step 1", plan.reason)
        self.assertIn("duty", plan.reason.lower())

    def test_two_steps_forward_ok_but_duty_interleaving_blocks(self):
        # Forward windows alone are satisfiable (step1 [0,100), step2 [80,100)),
        # but a failure at the end of step 2 forces step 1's rollback past its
        # duty window (closes 20), so the change is not releasable.
        steps = [
            step(10, (0, 100), 10, (0, 20), "first"),
            step(10, (80, 100), 5, (0, 200), "second"),
        ]
        plan = plan_change(steps, deadline=100)
        self.assertFalse(plan.feasible)
        self.assertIsNone(plan.start_time)
        self.assertIn("step 2", plan.reason)
        self.assertIn("duty", plan.reason.lower())

    def test_only_forward_window_ok_but_rollback_impossible(self):
        # Forward window is entirely after the duty window closes.
        steps = [step(10, (50, 80), 5, (0, 40))]
        plan = plan_change(steps, deadline=100)
        self.assertFalse(plan.feasible)
        self.assertIsNone(plan.start_time)
        self.assertTrue(plan.reason)

    def test_rollback_longer_than_duty_window(self):
        steps = [step(5, (0, 100), 50, (0, 40))]
        plan = plan_change(steps, deadline=100)
        self.assertFalse(plan.feasible)
        self.assertIn("empty", plan.reason)


class TestBoundaries(unittest.TestCase):
    def test_half_open_upper_bound_exclusive(self):
        # Window end is exclusive: forward of 10 in [0,10) is allowed ending 10;
        # in [0,9] it cannot fit -> row is invalid at construction time? No:
        # [0,9) length 9 < 10 -> infeasible, not invalid input.
        steps = [step(10, (0, 9), 1, (0, 100))]
        plan = plan_change(steps, deadline=100)
        self.assertFalse(plan.feasible)

    def test_exact_fit_is_allowed(self):
        steps = [step(10, (0, 10), 5, (10, 15))]
        plan = plan_change(steps, deadline=10)
        self.assertTrue(plan.feasible)
        self.assertEqual(plan.start_time, 0)
        self.assertEqual(plan.failures[0].rollbacks[0].start, 10)
        self.assertEqual(plan.failures[0].rollbacks[0].end, 15)

    def test_deadline_inclusive(self):
        steps = [step(10, (0, 100), 1, (0, 200))]
        self.assertTrue(plan_change(steps, deadline=10).feasible)
        # deadline itself is allowed (end <= deadline)
        p = plan_change(steps, deadline=10)
        self.assertEqual(p.forward[0].end, 10)

    def test_zero_duration_step(self):
        steps = [step(0, (0, 50), 0, (0, 50))]
        plan = plan_change(steps, deadline=50)
        self.assertTrue(plan.feasible)
        self.assertEqual(plan.start_time, 50)
        self.assertEqual(verify_plan(steps, 50, plan), [])

    def test_invalid_interval_construction(self):
        with self.assertRaises(ValueError):
            Interval(10, 10)
        with self.assertRaises(ValueError):
            Interval(10, 5)
        with self.assertRaises(TypeError):
            Interval(0.0, 5)


class TestInvalidInput(unittest.TestCase):
    def test_negative_duration(self):
        with self.assertRaises(ValueError):
            step(-1, (0, 10), 1, (0, 10))

    def test_bool_duration_rejected(self):
        with self.assertRaises(TypeError):
            step(True, (0, 10), 1, (0, 10))

    def test_float_duration_rejected(self):
        with self.assertRaises(TypeError):
            step(1.5, (0, 10), 1, (0, 10))

    def test_deadline_type(self):
        with self.assertRaises(TypeError):
            plan_change([step(1, (0, 10), 1, (0, 10))], deadline=5.0)
        with self.assertRaises(ValueError):
            plan_change([step(1, (0, 10), 1, (0, 10))], deadline=-1)

    def test_non_step_rejected(self):
        with self.assertRaises(TypeError):
            plan_change([object()], deadline=10)  # type: ignore[list-item]


class TestBruteForceCrossCheck(unittest.TestCase):
    """Compare against an exhaustive search on many small cases."""

    def _random_cases(self):
        import random

        rng = random.Random(20261122)
        cases = []
        # Mixed generator: small windows and short duty windows exercise both
        # feasible and infeasible boundaries.
        for _ in range(400):
            n = rng.randint(1, 3)
            steps = []
            for _ in range(n):
                ws = rng.randint(0, 12)
                we = ws + rng.randint(1, 10)
                ds = rng.randint(0, 14)
                de = ds + rng.randint(1, 20)
                steps.append(
                    step(
                        rng.randint(0, 6),
                        (ws, we),
                        rng.randint(0, 6),
                        (ds, de),
                    )
                )
            deadline = rng.randint(0, 25)
            cases.append((steps, deadline))
        # Feasible-biased generator: generous duty windows so the optimal
        # latest-start logic is stressed rather than early rejection.
        for _ in range(400):
            n = rng.randint(1, 4)
            steps = []
            for _ in range(n):
                ws = rng.randint(0, 10)
                we = ws + rng.randint(4, 15)
                ds = rng.randint(0, 5)
                de = ds + rng.randint(10, 40)
                steps.append(
                    step(
                        rng.randint(0, 5),
                        (ws, we),
                        rng.randint(0, 4),
                        (ds, de),
                    )
                )
            deadline = rng.randint(5, 30)
            cases.append((steps, deadline))
        return cases

    def test_matches_exhaustive_search(self):
        feasible_count = 0
        for idx, (steps, deadline) in enumerate(self._random_cases()):
            with self.subTest(case=idx):
                plan = plan_change(steps, deadline)
                expected = brute_force_latest_start(steps, deadline)
                if expected is None:
                    self.assertFalse(plan.feasible)
                    self.assertIsNone(plan.start_time)
                    self.assertTrue(plan.reason)
                else:
                    feasible_count += 1
                    self.assertTrue(plan.feasible, msg=f"reason={plan.reason}")
                    self.assertEqual(plan.start_time, expected)
                    self.assertEqual(verify_plan(steps, deadline, plan), [])
        # Guard against a generator that silently stops producing feasible cases.
        self.assertGreater(feasible_count, 200)


class TestVerifyCatchesBadPlans(unittest.TestCase):
    def test_verify_detects_deadline_violation(self):
        steps = [step(10, (0, 100), 1, (0, 200))]
        plan = plan_change(steps, deadline=100)
        bad = ChangePlan(
            feasible=True,
            start_time=plan.start_time,
            deadline=100,
            forward=plan.forward,
            failures=plan.failures,
        )
        # A plan that claims a tighter deadline than the placement satisfies.
        problems = verify_plan(steps, 50, bad)
        self.assertTrue(any("deadline" in p for p in problems))

    def test_verify_detects_rollback_out_of_duty(self):
        steps = [step(10, (0, 100), 5, (0, 100))]
        plan = plan_change(steps, deadline=100)
        # Rebuild with a rollback shifted outside the duty window.
        from change_scheduler import FailurePlan, RollbackAction

        bad_rollback = RollbackAction(step_index=1, name="", start=100, end=105)
        bad_failure = FailurePlan(failed_step=1, failure_time=100, rollbacks=(bad_rollback,))
        bad = ChangePlan(
            feasible=True,
            start_time=plan.start_time,
            deadline=100,
            forward=plan.forward,
            failures=(bad_failure,),
        )
        problems = verify_plan(steps, 100, bad)
        self.assertTrue(any("duty window" in p for p in problems))


class TestCli(unittest.TestCase):
    def test_cli_reads_file_and_reports_plan(self):
        problem = {
            "deadline": 100,
            "steps": [
                {
                    "name": "a",
                    "duration": 10,
                    "window": [0, 100],
                    "rollback_duration": 5,
                    "duty_window": [0, 200],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "problem.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(problem, fh)
            out = subprocess.run(
                [sys.executable, "-m", "change_scheduler", path],
                cwd=WORKSPACE,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(out.returncode, 0, msg=out.stderr)
        data = json.loads(out.stdout)
        self.assertTrue(data["feasible"])
        self.assertEqual(data["start_time"], 90)
        self.assertEqual(data["verification_problems"], [])

    def test_cli_infeasible_exit_zero_with_flag(self):
        problem = {
            "deadline": 100,
            "steps": [
                {
                    "duration": 10,
                    "window": [50, 80],
                    "rollback_duration": 5,
                    "duty_window": [0, 40],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "p.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(problem, fh)
            out = subprocess.run(
                [sys.executable, "-m", "change_scheduler", path],
                cwd=WORKSPACE,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(out.returncode, 0, msg=out.stderr)
        data = json.loads(out.stdout)
        self.assertFalse(data["feasible"])
        self.assertIsNone(data["start_time"])


if __name__ == "__main__":
    unittest.main()
