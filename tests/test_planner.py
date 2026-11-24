import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from blockcache import (
    CachePlanner,
    InfeasiblePlanError,
    InitialResident,
    Page,
    Pin,
    Read,
    Unpin,
    Write,
)


class PlannerBasicTests(unittest.TestCase):
    def test_read_miss_occupies_slot_for_fixed_ticks(self):
        planner = CachePlanner([Page("A", 2)], 2, read_miss_ticks=4, writeback_ticks=3)
        plan = planner.plan([Read("A")])

        self.assertEqual(plan.makespan_ticks, 4)
        self.assertEqual(plan.writeback_bytes, 0)
        self.assertEqual(plan.occupancy[-1].used_bytes, 2)
        self.assertTrue(any(e.op == "read_miss" and e.duration_ticks == 4 for e in plan.events))

    def test_dirty_write_is_persisted_and_counted(self):
        planner = CachePlanner([Page("A", 3)], 3, read_miss_ticks=4, writeback_ticks=2)
        plan = planner.plan([Write("A", "answer")])

        self.assertEqual(plan.makespan_ticks, 2)
        self.assertEqual(plan.writeback_bytes, 3)
        self.assertEqual(plan.final_persistent_values["A"], "answer")
        self.assertEqual(len(plan.writebacks), 1)
        wb = plan.writebacks[0]
        self.assertEqual((wb.generation, wb.started_at_tick, wb.completed_at_tick), (1, 0, 2))
        self.assertFalse(wb.stale_completion)
        self.assertTrue(wb.clean_after_completion)

    def test_read_hit_on_dirty_page_does_not_clean_it(self):
        planner = CachePlanner([Page("A", 1)], 1, writeback_ticks=2)
        plan = planner.plan([Write("A", "v"), Read("A")])
        self.assertEqual(plan.writeback_bytes, 1)
        self.assertEqual(plan.makespan_ticks, 2)
        self.assertTrue(any(w.page == "A" and w.generation == 1 for w in plan.writebacks))

    def test_lexicographic_tie_breaks_to_fewer_bytes(self):
        pages = [Page("A", 1), Page("B", 1)]
        planner = CachePlanner(pages, 2, read_miss_ticks=5, writeback_ticks=2)
        # Writing A early would persist gen 1 and then gen 2, two page writes.
        # Waiting until after the read and coalescing to gen 2 finishes at the
        # same tick but writes only one generation.
        plan = planner.plan([Write("A", "v1"), Read("B"), Write("A", "v2")])

        self.assertEqual(plan.makespan_ticks, 7)
        self.assertEqual(plan.writeback_bytes, 1)
        self.assertEqual([w.generation for w in plan.writebacks], [2])
        self.assertEqual(plan.final_persistent_values["A"], "v2")

    def test_capacity_boundary_exact_fit_is_legal(self):
        pages = [Page("A", 2), Page("B", 3)]
        planner = CachePlanner(
            pages,
            5,
            read_miss_ticks=4,
            initial_cache={"A": InitialResident()},
        )
        plan = planner.plan([Read("B")])
        self.assertEqual(plan.makespan_ticks, 4)
        self.assertEqual(plan.occupancy[-1].used_bytes, 5)

    def test_pinned_page_blocks_eviction_then_unpin_allows_it(self):
        pages = [Page("A", 2), Page("B", 2)]
        pinned = CachePlanner(
            pages,
            2,
            read_miss_ticks=3,
            initial_cache={"A": InitialResident(pins=1)},
        )
        with self.assertRaises(InfeasiblePlanError):
            pinned.plan([Read("B")])

        planner = CachePlanner(pages, 2, read_miss_ticks=3)
        plan = planner.plan([Pin("A"), Unpin("A"), Read("B")])
        # Pin/unpin balance is legal, but A was never resident in this fresh
        # planner; pin bookkeeping remains and unpin clears it.  The miss can
        # allocate because the cache starts empty.
        self.assertEqual(plan.makespan_ticks, 3)

        resident_planner = CachePlanner(
            pages,
            2,
            read_miss_ticks=3,
            initial_cache={"A": InitialResident(pins=1)},
        )
        plan2 = resident_planner.plan([Unpin("A"), Read("B")])
        self.assertEqual(plan2.makespan_ticks, 3)
        self.assertIn(("A",), [tuple(x.page for x in plan2.events if x.__class__.__name__ == "EvictEvent")])

    def test_tuple_request_api(self):
        planner = CachePlanner([Page("A", 1)], 1, read_miss_ticks=2, writeback_ticks=1)
        plan = planner.plan([("W", "A", 7)])
        self.assertEqual(plan.final_persistent_values["A"], 7)
        self.assertEqual(plan.makespan_ticks, 1)

    def test_more_than_six_pages_rejected(self):
        with self.assertRaises(ValueError):
            CachePlanner([Page(f"P{i}", 1) for i in range(7)], 8)


if __name__ == "__main__":
    unittest.main()
