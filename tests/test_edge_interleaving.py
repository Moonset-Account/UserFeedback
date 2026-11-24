import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from blockcache import (
    CachePlanner,
    Checkpoint,
    InitialResident,
    Page,
    Pin,
    Read,
    TickInterpreter,
    Unpin,
    Write,
)


class BoundaryAndInterleavingTests(unittest.TestCase):
    def test_single_writeback_channel_serializes_two_dirty_pages(self):
        pages = [Page("A", 1, writeback_ticks=2), Page("B", 1, writeback_ticks=3)]
        planner = CachePlanner(pages, 2, writeback_ticks=99)
        plan = planner.plan([Write("A", "a"), Write("B", "b")])

        self.assertEqual(plan.makespan_ticks, 5)
        self.assertEqual(plan.writeback_bytes, 2)
        intervals = sorted((w.started_at_tick, w.completed_at_tick) for w in plan.writebacks)
        self.assertEqual(intervals[0][1], intervals[1][0])
        report = TickInterpreter(planner, [Write("A", "a"), Write("B", "b")]).run_all(plan)
        self.assertTrue(report.ok)

    def test_pin_unpin_interleaving_controls_eviction(self):
        pages = [Page("A", 1), Page("B", 1), Page("C", 1)]
        planner = CachePlanner(
            pages,
            2,
            read_miss_ticks=4,
            initial_cache={"A": InitialResident(pins=1), "B": InitialResident()},
        )
        # While A is pinned, B is the only victim.  Unpin A only after C has
        # been loaded; this workload still forces B eviction.
        requests = [Read("C"), Unpin("A"), Read("A")]
        plan = planner.plan(requests)
        report = TickInterpreter(planner, requests).run_all(plan)
        self.assertTrue(report.ok, report.violations)

        evicted = [e.page for e in plan.events if e.__class__.__name__ == "EvictEvent"]
        self.assertEqual(evicted[0], "B")
        self.assertLessEqual(plan.makespan_ticks, 12)

    def test_exact_capacity_and_one_byte_pages(self):
        pages = [Page("A", 1), Page("B", 1), Page("C", 1)]
        planner = CachePlanner(pages, 2, read_miss_ticks=1, writeback_ticks=1)
        plan = planner.plan([Read("A"), Read("B"), Read("C")])
        report = TickInterpreter(planner, [Read("A"), Read("B"), Read("C")]).run_all(plan)
        self.assertTrue(report.ok)
        self.assertEqual(max(s.used_bytes for s in report.trace), 2)
        self.assertTrue(all(s.used_bytes <= 2 for s in report.trace))

    def test_recovery_at_writeback_completion_keeps_historical_bytes(self):
        planner = CachePlanner([Page("A", 1)], 1, writeback_ticks=2)
        requests = [Write("A", "durable")]
        full = planner.plan(requests)
        self.assertEqual(full.makespan_ticks, 2)

        prefix = TickInterpreter(planner, requests)
        checkpoint = prefix.run_prefix_until_tick(full.events, target_tick=2)
        self.assertIsNone(checkpoint.active_writeback)
        self.assertEqual(checkpoint.bytes_already_written, 1)

        resumed = planner.resume(checkpoint, requests)
        self.assertEqual(resumed.events, ())
        self.assertEqual(resumed.makespan_ticks, 2)
        self.assertEqual(resumed.writeback_bytes, 1)
        verifier = TickInterpreter(planner, requests, checkpoint=checkpoint)
        report = verifier.run_all(resumed)
        self.assertTrue(report.ok)
        self.assertEqual(report.final_persistent_values["A"], "durable")

    def test_concurrent_foreground_and_background_both_advance_together(self):
        pages = [Page("A", 2, writeback_ticks=2), Page("B", 2)]
        planner = CachePlanner(pages, 4, read_miss_ticks=2, writeback_ticks=2)
        requests = [Write("A", "a"), Read("B")]
        plan = planner.plan(requests)
        report = TickInterpreter(planner, requests).run_all(plan)

        overlapping = [
            s
            for s in report.trace
            if s.active_writeback is not None and s.foreground_remaining is not None
        ]
        self.assertTrue(overlapping)
        self.assertEqual(plan.makespan_ticks, 2)
        self.assertEqual(plan.final_persistent_values["A"], "a")


if __name__ == "__main__":
    unittest.main()
