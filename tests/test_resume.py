import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from blockcache import (
    ActiveWritebackCheckpoint,
    CachePlanner,
    Checkpoint,
    InitialResident,
    Page,
    Read,
    ResidentCheckpoint,
    TickInterpreter,
    Write,
)


class ResumePlanningTests(unittest.TestCase):
    def _stale_checkpoint(self):
        return Checkpoint(
            at_tick=2,
            cursor=1,
            residents={
                "P": ResidentCheckpoint(
                    generation=1, dirty=True, ready=True, value="v1"
                )
            },
            generation={"P": 1, "Q": 0},
            stable_generation={"P": 0, "Q": 0},
            persistent_values={"P": "v0", "Q": "Q#gen0"},
            pins={},
            active_writeback=ActiveWritebackCheckpoint(
                page="P",
                generation=1,
                remaining_ticks=3,
                duration_ticks=5,
                byte_count=2,
                value="v1",
                job_id=1,
                started_at_tick=0,
            ),
            bytes_already_written=0,
            next_job_id=2,
        )

    def test_resume_mid_writeback_handles_stale_receipt(self):
        pages = [Page("P", 2, writeback_ticks=5), Page("Q", 2, writeback_ticks=5)]
        planner = CachePlanner(pages, 4, read_miss_ticks=8, writeback_ticks=5)
        checkpoint = self._stale_checkpoint()
        requests = [Write("P", "v1"), Write("P", "v2"), Read("Q")]

        suffix_plan = planner.resume(checkpoint, requests)
        # At tick 2 the new write and Q miss start.  Old wb ends at 5 but is
        # stale; gen 2 then occupies channel [5,10], overlapping Q's tail and
        # completing together at tick 10.
        self.assertEqual(suffix_plan.makespan_ticks, 10)
        self.assertEqual(suffix_plan.writeback_bytes, 4)
        self.assertEqual(suffix_plan.final_persistent_values["P"], "v2")

        fresh = TickInterpreter(planner, requests, checkpoint=checkpoint)
        report = fresh.run_all(suffix_plan)
        self.assertTrue(report.ok, report.violations)
        self.assertEqual(report.final_persistent_values["P"], "v2")
        stale = [w for w in suffix_plan.writebacks if w.stale_completion]
        current = [w for w in suffix_plan.writebacks if not w.stale_completion]
        self.assertEqual([w.generation for w in stale], [1])
        self.assertFalse(stale[0].clean_after_completion)
        self.assertEqual([w.generation for w in current], [2])
        self.assertTrue(current[0].clean_after_completion)

    def test_resume_from_prefix_of_uninterrupted_optimal_plan(self):
        pages = [
            Page("A", 1),
            Page("M", 2),
            Page("B", 3),
            Page("X", 4),
            Page("L", 5),
        ]
        planner = CachePlanner(
            pages,
            11,
            read_miss_ticks=5,
            writeback_ticks=3,
            initial_cache={
                "A": InitialResident(pins=1),
                "B": InitialResident(pins=1),
                "L": InitialResident(),
            },
        )
        requests = [Write("L", "v1"), Read("M"), Read("X"), Read("L")]
        full_plan = planner.plan(requests)
        self.assertEqual(full_plan.makespan_ticks, 15)

        prefix_runner = TickInterpreter(planner, requests)
        checkpoint = prefix_runner.run_prefix_until_tick(full_plan.events, target_tick=1)
        self.assertEqual(checkpoint.at_tick, 1)
        self.assertIsNotNone(checkpoint.active_writeback)
        self.assertEqual(checkpoint.active_writeback.page, "L")
        self.assertEqual(checkpoint.active_writeback.remaining_ticks, 2)
        self.assertIsNotNone(checkpoint.foreground)

        resumed_plan = planner.resume(checkpoint, requests)
        self.assertEqual(resumed_plan.makespan_ticks, full_plan.makespan_ticks)
        verifier = TickInterpreter(planner, requests, checkpoint=checkpoint)
        report = verifier.run_all(resumed_plan)
        self.assertTrue(report.ok, report.violations)
        self.assertEqual(report.final_persistent_values["L"], "v1")
        self.assertTrue(all(s.used_bytes <= 11 for s in report.trace))


if __name__ == "__main__":
    unittest.main()
