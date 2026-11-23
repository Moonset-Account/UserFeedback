"""Randomized cross-validation against the exhaustive oracle and checks that
the whole-horizon service impact is internally consistent.
"""

from __future__ import annotations

import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from emergency_scheduler import enumerate_best, simulate, solve  # noqa: E402

from tests import fixtures as fx  # noqa: E402


class FuzzAgainstExhaustiveTest(unittest.TestCase):
    def test_matches_exhaustive_on_random_small_instances(self) -> None:
        rng = random.Random(20261123)
        checked = 0
        feasible_checked = 0
        infeasible_checked = 0
        parallel_windows_seen = 0
        for _ in range(120):
            problem = fx.random_problem(rng)
            exhaustive = enumerate_best(problem, state_limit=150_000)
            result = solve(problem)
            checked += 1
            self.assertEqual(result.feasible, exhaustive.feasible)
            if exhaustive.feasible:
                feasible_checked += 1
                self.assertAlmostEqual(
                    result.weighted_objective,
                    exhaustive.weighted_objective,
                    places=9,
                )
                self.assertEqual(result.makespan, exhaustive.makespan)
                solver_starts = {
                    seg.stage_id: seg.start for seg in result.segments
                }
                self.assertEqual(solver_starts, exhaustive.starts)

                # every reported schedule must satisfy all constraints under
                # the independent sweep simulation
                assignments = [
                    (
                        next(
                            s
                            for c in problem.components
                            for s in c.stages
                            if s.stage_id == sid
                        ),
                        st,
                    )
                    for sid, st in solver_starts.items()
                ]
                report = simulate(problem, assignments)
                self.assertTrue(report.feasible, msg=str(report.violation))
                self.assertEqual(result.total_unmet, report.total_unmet)

                # detect at least one genuinely parallel window in the corpus
                windows: dict[int, list[str]] = {}
                for seg in result.segments:
                    for t in range(seg.start, seg.end):
                        windows.setdefault(t, []).append(seg.stage_id)
                if any(len(ids) > 1 for ids in windows.values()):
                    parallel_windows_seen += 1
            else:
                infeasible_checked += 1
                self.assertIsNotNone(result.failure)
        # both classes and parallel interleavings must actually appear,
        # otherwise the test is vacuous
        self.assertGreater(feasible_checked, 40)
        self.assertGreater(infeasible_checked, 10)
        self.assertGreater(parallel_windows_seen, 5)
        self.assertEqual(checked, 120)


class ServiceImpactAggregationTest(unittest.TestCase):
    def test_total_impact_matches_tick_by_tick_simulation(self) -> None:
        problem = fx.stop_redundant_problem()
        result = solve(problem)
        assignments = [
            (
                next(
                    s
                    for c in problem.components
                    for s in c.stages
                    if s.stage_id == seg.stage_id
                ),
                seg.start,
            )
            for seg in result.segments
        ]
        report = simulate(problem, assignments)
        self.assertTrue(report.feasible)

        # totals equal the sum of per-tick unmet demand
        manual_total = {
            sid: sum(tick[sid] for tick in report.unmet_trace)
            for sid in result.total_unmet
        }
        self.assertEqual(manual_total, report.total_unmet)
        self.assertEqual(result.total_unmet, report.total_unmet)

        # weighted objective equals weight * integrated unmet, summed
        weight = {s.service_id: s.weight for s in problem.services}
        recomputed = sum(
            value * weight[sid] for sid, value in result.total_unmet.items()
        )
        self.assertAlmostEqual(recomputed, result.weighted_objective, places=9)

    def test_segment_impacts_partition_each_tick_under_serial_work(self) -> None:
        # on serial schedules each tick belongs to exactly one active stage,
        # so the per-segment integrated unmet demands must sum to the totals
        problem = fx.spare_release_problem()
        result = solve(problem)
        for sid, total in result.total_unmet.items():
            segment_sum = sum(seg.unmet_demand[sid] for seg in result.segments)
            self.assertAlmostEqual(segment_sum, total, places=9)

        # hand-computed check: demand 10, no capacity until t=2 (B restores 2),
        # then 2 units until t=5 (A restores 8), then fully satisfied
        b_segment = next(s for s in result.segments if s.stage_id == "b1")
        a_segment = next(s for s in result.segments if s.stage_id == "a1")
        self.assertEqual(b_segment.unmet_demand, {"s1": 20.0})
        self.assertEqual(a_segment.unmet_demand, {"s1": 24.0})
        self.assertEqual(result.total_unmet, {"s1": 44.0})

    def test_overlapping_segments_share_but_total_is_counted_once(self) -> None:
        problem = fx.interleaving_problem()
        result = solve(problem)
        # a2 and b1 overlap; segment-level figures double count that window,
        # while total_unmet must still be the authoritative once-only number
        assignments = [
            (
                next(
                    s
                    for c in problem.components
                    for s in c.stages
                    if s.stage_id == seg.stage_id
                ),
                seg.start,
            )
            for seg in result.segments
        ]
        report = simulate(problem, assignments)
        self.assertEqual(result.total_unmet, report.total_unmet)
        self.assertGreater(
            sum(seg.unmet_demand["s1"] for seg in result.segments),
            result.total_unmet["s1"],
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
