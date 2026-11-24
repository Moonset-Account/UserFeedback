import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from blockcache import (
    CachePlanner,
    InitialResident,
    Page,
    Pin,
    Read,
    TickInterpreter,
    Write,
    access_distance_greedy_plan,
)


class GreedyScenarioTests(unittest.TestCase):
    def build_workload(self):
        # Five distinct sizes 1..5.  A and B are pinned at the critical miss.
        # L (size 5) is the only unpinned resident whose eviction alone frees
        # enough room for X (size 4); M (size 2) is too small by itself.
        pages = [
            Page("A", 1),
            Page("M", 2),
            Page("B", 3),
            Page("X", 4),
            Page("L", 5),
        ]
        planner = CachePlanner(
            pages,
            capacity=11,
            read_miss_ticks=5,
            writeback_ticks=3,
            initial_cache={
                "A": InitialResident(),
                "B": InitialResident(),
                "L": InitialResident(),
            },
        )
        requests = [
            Pin("A"),
            Pin("B"),
            Write("L", "dirty-late-page"),
            Read("M"),
            Read("X"),
            Read("M"),  # M is reused before L.
            Read("A"),
            Read("B"),
            Read("L"),  # L is globally latest next read/write.
        ]
        return planner, requests

    def test_farthest_access_greedy_loses_to_early_writeback(self):
        planner, requests = self.build_workload()
        optimal = planner.plan(requests)
        greedy = access_distance_greedy_plan(planner, requests)

        optimal_report = TickInterpreter(planner, requests).run_all(optimal)
        greedy_report = TickInterpreter(planner, requests).run_all(greedy)
        self.assertTrue(optimal_report.ok, optimal_report.violations)
        self.assertTrue(greedy_report.ok, greedy_report.violations)

        # Optimal overlaps L's writeback [0,3) with M's read miss [0,5).
        self.assertEqual(optimal.makespan_ticks, 15)
        self.assertEqual(greedy.makespan_ticks, 18)
        self.assertLess(optimal.makespan_ticks, greedy.makespan_ticks)

        optimal_wb = optimal.writebacks[0]
        greedy_wb = greedy.writebacks[0]
        self.assertEqual((optimal_wb.page, optimal_wb.started_at_tick), ("L", 0))
        self.assertEqual((greedy_wb.page, greedy_wb.started_at_tick), ("L", 5))

        foreground = {(x.item_id, x.start_tick, x.end_tick) for x in optimal.timeline if x.resource == "foreground"}
        self.assertTrue(any(":read:M" in item and s == 0 and e == 5 for item, s, e in foreground))
        channel = [(w.started_at_tick, w.completed_at_tick) for w in optimal.writebacks if w.page == "L" and w.generation == 1]
        self.assertEqual(channel, [(0, 3)])

    def test_scenario_identity_is_exactly_described(self):
        planner, requests = self.build_workload()
        # Before Read X, L has the latest next read/write use.  A and B are
        # pinned by prefix requests; M is legal but too small by itself.
        next_use = {}
        for candidate in ("M", "A", "B", "L"):
            next_use[candidate] = next(
                j for j, r in enumerate(requests)
                if r.__class__.__name__ in {"Read", "Write"} and r.page == candidate
                and j > requests.index(next(r for r in requests if getattr(r, "page", None) == "X"))
            )
        self.assertEqual(max(next_use, key=next_use.get), "L")
        self.assertGreater(next_use["L"], next_use["B"])
        # M alone leaves capacity 11-2+4=13 > 11; L alone fits.
        self.assertGreater(11 - planner.pages["M"].size + planner.pages["X"].size, 11)
        self.assertLessEqual(11 - planner.pages["L"].size + planner.pages["X"].size, 11)
        self.assertEqual({p.size for p in planner.pages.values()}, {1, 2, 3, 4, 5})


if __name__ == "__main__":
    unittest.main()
