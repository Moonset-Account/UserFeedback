import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from blockcache import (
    CachePlanner,
    EvictEvent,
    Page,
    PlanVerificationError,
    Read,
    TickInterpreter,
    Write,
)


class TickInterpreterTests(unittest.TestCase):
    def test_generated_plan_passes_tick_by_tick_checks(self):
        pages = [Page("A", 2, writeback_ticks=3), Page("B", 3, writeback_ticks=2)]
        planner = CachePlanner(pages, 5, read_miss_ticks=4)
        requests = [Write("A", "a"), Read("B"), Write("A", "a2")]
        plan = planner.plan(requests)

        interpreter = TickInterpreter(planner, requests)
        report = interpreter.run_all(plan)

        self.assertTrue(report.ok, report.violations)
        self.assertEqual(report.violations, ())
        self.assertLessEqual(report.peak_used_bytes, 5)
        self.assertEqual(report.final_persistent_values["A"], "a2")
        # Every recorded occupancy sample respects capacity.
        self.assertTrue(all(0 <= s.used_bytes <= s.capacity_bytes for s in report.trace))
        # Single channel: writeback intervals never overlap.
        intervals = [(w.started_at_tick, w.completed_at_tick) for w in plan.writebacks]
        intervals.sort()
        for (s1, e1), (s2, e2) in zip(intervals, intervals[1:]):
            self.assertLessEqual(e1, s2)

    def test_interpreter_rejects_eviction_of_absent_page(self):
        planner = CachePlanner([Page("A", 1)], 1)
        interpreter = TickInterpreter(planner, [Read("A")])
        with self.assertRaises(PlanVerificationError):
            interpreter.execute_event(EvictEvent(0, "A", 1))

    def test_intermediate_occupancy_is_recorded_during_overlap(self):
        pages = [Page("A", 2, writeback_ticks=3), Page("B", 2)]
        planner = CachePlanner(pages, 4, read_miss_ticks=5)
        plan = planner.plan([Write("A", "v"), Read("B")])
        report = TickInterpreter(planner, [Write("A", "v"), Read("B")]).run_all(plan)

        during = [s for s in report.trace if s.active_writeback is not None]
        self.assertTrue(during)
        sample = during[0]
        self.assertLessEqual(sample.used_bytes, 4)
        self.assertIsNotNone(sample.foreground_remaining)

    def test_final_persistence_check_detects_missing_flush(self):
        planner = CachePlanner([Page("A", 1)], 1, writeback_ticks=1)
        interpreter = TickInterpreter(planner, [Write("A", "v")])
        # Begin the write but do not queue/run writeback.
        from blockcache import BeginRequestEvent

        interpreter.execute_event(BeginRequestEvent(0, 0, "write", "A", 0))
        with self.assertRaises(PlanVerificationError):
            interpreter.assert_goal(1)


if __name__ == "__main__":
    unittest.main()
