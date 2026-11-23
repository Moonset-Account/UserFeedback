"""Branch-and-bound solver tests, cross-checked against exhaustive search."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from emergency_scheduler import enumerate_best, simulate, solve  # noqa: E402

from tests import fixtures as fx  # noqa: E402


class SolverMatchesExhaustiveTest(unittest.TestCase):
    def _assert_matches_exhaustive(self, problem) -> None:
        result = solve(problem)
        exhaustive = enumerate_best(problem)
        self.assertTrue(result.feasible)
        self.assertTrue(exhaustive.feasible)
        self.assertAlmostEqual(
            result.weighted_objective,
            exhaustive.weighted_objective,
            places=9,
        )
        self.assertEqual(result.makespan, exhaustive.makespan)
        solver_starts = {seg.stage_id: seg.start for seg in result.segments}
        self.assertEqual(solver_starts, exhaustive.starts)

        # the returned schedule must pass the independent sweep simulation
        assignments = [
            (next(s for c in problem.components for s in c.stages if s.stage_id == sid), st)
            for sid, st in solver_starts.items()
        ]
        report = simulate(problem, assignments)
        self.assertTrue(report.feasible, msg=str(report.violation))
        self.assertAlmostEqual(
            report.weighted_objective, result.weighted_objective, places=9
        )
        self.assertEqual(report.makespan, result.makespan)

    def test_larger_recovery_repaired_first(self) -> None:
        # normal path: A restores 6 per tick vs B's 4, so A goes first.
        problem = fx.sequencing_problem()
        result = solve(problem)
        starts = {seg.stage_id: (seg.start, seg.end) for seg in result.segments}
        self.assertEqual(starts, {"a1": (0, 2), "b1": (2, 4)})
        # hand-computed integrated unmet: 2 ticks * 10 + 2 ticks * 4
        self.assertEqual(result.total_unmet, {"s1": 28.0})
        self.assertEqual(result.weighted_objective, 28.0)
        self.assertEqual(result.makespan, 4)
        self.assertEqual(result.completion_order, [(2, "A"), (4, "B")])
        self._assert_matches_exhaustive(problem)

    def test_small_recovery_first_frees_spare_for_big_repair(self) -> None:
        # the explicitly requested counter-intuitive ordering
        problem = fx.spare_release_problem()
        result = solve(problem)
        starts = {seg.stage_id: (seg.start, seg.end) for seg in result.segments}
        self.assertEqual(starts, {"b1": (0, 2), "a1": (2, 5)})
        # B first is the only feasible order: A needs the pump B releases
        self.assertEqual(result.completion_order, [(2, "B"), (5, "A")])
        self.assertEqual(result.total_unmet, {"s1": 44.0})
        self.assertEqual(result.weighted_objective, 44.0)
        self._assert_matches_exhaustive(problem)

        # the reverse order is infeasible because no pump exists at t=0
        reverse = [
            (problem.components[0].stages[0], 0),
            (problem.components[1].stages[0], 3),
        ]
        report = simulate(problem, reverse)
        self.assertFalse(report.feasible)
        self.assertEqual(report.violation.kind, "spare")
        self.assertEqual(report.violation.time, 0)

    def test_stopping_redundant_component_accounts_capacity(self) -> None:
        problem = fx.stop_redundant_problem()
        result = solve(problem)
        self.assertTrue(result.feasible)
        a_segment = next(seg for seg in result.segments if seg.stage_id == "a1")
        self.assertEqual(a_segment.stopped_components, ("C",))
        # B (no stop) must finish first so its 4 units back the risky A window
        self.assertLess(
            next(s.start for s in result.segments if s.stage_id == "b1"),
            a_segment.start,
        )
        self._assert_matches_exhaustive(problem)

    def test_multistage_precedence_and_no_early_capacity_credit(self) -> None:
        problem = fx.multistage_problem()
        result = solve(problem)
        a1 = next(s for s in result.segments if s.stage_id == "a1")
        a2 = next(s for s in result.segments if s.stage_id == "a2")
        # non-preemptive ordered stages, back to back without overlap
        self.assertEqual(a1.end, a2.start)
        self.assertGreater(a1.start, -1)
        # A's capacity is unavailable until a2 (its last stage) completes
        report = simulate(
            problem,
            [
                (problem.components[0].stages[0], a1.start),
                (problem.components[0].stages[1], a2.start),
                (problem.components[1].stages[0], next(
                    s.start for s in result.segments if s.stage_id == "b1"
                )),
            ],
        )
        self.assertTrue(report.feasible)
        for t in range(a1.start, a2.end):
            self.assertEqual(report.capacity_trace[t].get("s1", 0.0), 0.0)
        self._assert_matches_exhaustive(problem)

    def test_parallel_stage_interleaving(self) -> None:
        problem = fx.interleaving_problem()
        result = solve(problem)
        starts = {seg.stage_id: seg.start for seg in result.segments}
        # with two technicians, a2 and b1 can run in parallel once the single
        # spare has been returned at t=2
        self.assertEqual(starts["a2"], starts["b1"])
        self.assertNotEqual(starts["a1"], starts["b1"])
        self._assert_matches_exhaustive(problem)

    def test_repair_finishing_exactly_on_horizon_is_feasible(self) -> None:
        # boundary: stage end == horizon must be accepted (half-open interval)
        problem = fx.sequencing_problem()
        problem.horizon = 4
        result = solve(problem)
        self.assertTrue(result.feasible)
        self.assertEqual(result.makespan, 4)
        self._assert_matches_exhaustive(problem)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
