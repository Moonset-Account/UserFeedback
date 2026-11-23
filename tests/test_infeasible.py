"""Infeasibility and first-failure diagnostic tests."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from emergency_scheduler import first_failure, solve  # noqa: E402
from emergency_scheduler.brute_force import StateLimitReached  # noqa: E402

from tests import fixtures as fx  # noqa: E402


class InfeasibleTest(unittest.TestCase):
    def test_minimum_capacity_breach_reported_first(self) -> None:
        problem = fx.stop_violates_minimum_problem()
        result = solve(problem)
        self.assertFalse(result.feasible)
        self.assertEqual(result.segments, [])
        self.assertEqual(result.failure.kind, "min_capacity")
        self.assertEqual(result.failure.time, 0)
        self.assertTrue(result.failure.exact)
        # exact reachability analysis must agree with the solver's diagnostic
        failure = first_failure(problem)
        self.assertEqual(failure.kind, "min_capacity")
        self.assertEqual(failure.time, 0)
        self.assertEqual(result.failure.detail, failure.detail)

    def test_spare_starvation_reported(self) -> None:
        problem = fx.spare_starvation_problem()
        result = solve(problem)
        self.assertFalse(result.feasible)
        # with enough personnel for every stage, the spare is the unique
        # resource that no continuation can ever supply
        self.assertEqual(result.failure.kind, "spare")
        self.assertEqual(result.failure.time, 0)
        self.assertEqual(result.failure.related["spare_id"], "seal")
        self.assertTrue(result.failure.exact)

        from emergency_scheduler.brute_force import (
            _can_start,
            _initial_state,
            _start_blocker,
        )

        state = _initial_state(problem)
        stage = problem.components[0].stages[0]
        self.assertFalse(_can_start(problem, state, stage, 0))
        blocker = _start_blocker(problem, state, stage, 0)
        self.assertIsNotNone(blocker)
        self.assertEqual(blocker[0], "spare")
        self.assertEqual(blocker[2]["spare_id"], "seal")

        failure = first_failure(problem)
        self.assertEqual(failure.kind, "spare")
        self.assertEqual(failure.time, 0)

    def test_horizon_overflow_reported(self) -> None:
        problem = fx.horizon_infeasible_problem()
        result = solve(problem)
        self.assertFalse(result.feasible)
        self.assertEqual(result.failure.kind, "horizon")
        self.assertEqual(result.failure.time, problem.horizon)
        failure = first_failure(problem)
        self.assertEqual(failure.kind, "horizon")

    def test_first_failure_raises_on_feasible_instance(self) -> None:
        problem = fx.sequencing_problem()
        with self.assertRaises(ValueError):
            first_failure(problem)

    def test_state_limit_propagates_and_fallback_diagnoses(self) -> None:
        problem = fx.spare_starvation_problem()
        with self.assertRaises(StateLimitReached):
            first_failure(problem, state_limit=1)
        result = solve(problem, brute_force_limit=1)
        self.assertFalse(result.feasible)
        # fallback diagnostic is constructive and marked non-exact
        self.assertFalse(result.failure.exact)
        self.assertIn(result.failure.kind, {"spare", "horizon"})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
