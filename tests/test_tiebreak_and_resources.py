"""Tie-breaking and resource boundary tests."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from emergency_scheduler import Component, Problem, Service, Stage, solve  # noqa: E402


class TieBreakTest(unittest.TestCase):
    def test_secondary_makespan_objective_prefers_earlier_completion(self) -> None:
        # Two identical-capacity failed units: primary cost is identical no
        # matter the order, so the solver must minimize the makespan and then
        # order component completion by component id.
        a = Component("A", {"s1": 3.0}, [Stage("a1", "A", 2, personnel=1)])
        b = Component("B", {"s1": 3.0}, [Stage("b1", "B", 2, personnel=1)])
        problem = Problem(
            components=[a, b],
            services=[Service("s1", demand=6.0)],
            horizon=8,
            personnel_capacity=1,
        )
        result = solve(problem)
        self.assertTrue(result.feasible)
        # both orders have makespan 4; component id tie break selects A first
        self.assertEqual(result.completion_order, [(2, "A"), (4, "B")])
        starts = {seg.stage_id: seg.start for seg in result.segments}
        self.assertEqual(starts, {"a1": 0, "b1": 2})

    def test_personnel_capacity_limits_parallelism(self) -> None:
        a = Component("A", {"s1": 1.0}, [Stage("a1", "A", 2, personnel=2)])
        b = Component("B", {"s1": 1.0}, [Stage("b1", "B", 2, personnel=2)])
        problem = Problem(
            components=[a, b],
            services=[Service("s1", demand=2.0)],
            horizon=6,
            personnel_capacity=2,
        )
        result = solve(problem)
        self.assertTrue(result.feasible)
        # each stage needs the whole crew, so the two cannot overlap
        intervals = [(seg.start, seg.end) for seg in result.segments]
        intervals.sort()
        self.assertEqual(intervals[0][1], intervals[1][0])

    def test_segment_reports_occupied_resources(self) -> None:
        a = Component(
            "A",
            {"s1": 4.0},
            [
                Stage(
                    "a1",
                    "A",
                    duration=2,
                    personnel=2,
                    spare_needs={"v": 1},
                    spare_releases={"kit": 1},
                )
            ],
        )
        problem = Problem(
            components=[a],
            services=[Service("s1", demand=4.0)],
            horizon=4,
            personnel_capacity=2,
            initial_spares={"v": 1},
        )
        result = solve(problem)
        self.assertTrue(result.feasible)
        segment = result.segments[0]
        self.assertEqual(segment.start, 0)
        self.assertEqual(segment.end, 2)
        self.assertEqual(segment.personnel, 2)
        self.assertEqual(segment.spare_needs, {"v": 1})
        self.assertEqual(segment.spare_releases, {"kit": 1})
        # unmet demand integrated over the two-tick repair window
        self.assertEqual(segment.unmet_demand, {"s1": 8.0})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
