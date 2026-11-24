import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from blockcache import (  # noqa: E402
    CachePlanner,
    InfeasiblePlanError,
    Page,
    Pin,
    Read,
    TickInterpreter,
    Unpin,
    Write,
    access_distance_greedy_plan,
)


class RandomInvariantTests(unittest.TestCase):
    def test_deterministic_random_workloads_satisfy_invariants(self):
        # Deterministic seed: this is a compact property sweep, not a
        # statistical benchmark.  It exercises mixed sizes, generations, pins,
        # evictions, and channel overlap.
        for trial in range(80):
            rng = random.Random(0xB10C + trial)
            n_pages = rng.randint(1, 6)
            pages = [
                Page(f"P{i}", rng.randint(1, 4), writeback_ticks=rng.randint(1, 3))
                for i in range(n_pages)
            ]
            total_size = sum(p.size for p in pages)
            capacity = rng.randint(max(p.size for p in pages), total_size + 2)
            planner = CachePlanner(
                pages,
                capacity,
                read_miss_ticks=rng.randint(1, 4),
                writeback_ticks=2,
                max_search_states=100_000,
            )

            pin_counts: dict[str, int] = {}
            requests = []
            for j in range(rng.randint(1, 14)):
                pid = rng.choice(pages).id
                choices = ["R", "W", "P"]
                if any(count > 0 for count in pin_counts.values()):
                    choices.append("U")
                op = rng.choice(choices)
                if op == "R":
                    requests.append(Read(pid))
                elif op == "W":
                    requests.append(Write(pid, f"t{trial}-w{j}"))
                elif op == "P":
                    requests.append(Pin(pid))
                    pin_counts[pid] = pin_counts.get(pid, 0) + 1
                else:
                    pinned_ids = [p for p, count in pin_counts.items() if count > 0]
                    unpin_id = rng.choice(pinned_ids)
                    requests.append(Unpin(unpin_id))
                    pin_counts[unpin_id] -= 1
                    if pin_counts[unpin_id] == 0:
                        del pin_counts[unpin_id]

            try:
                plan = planner.plan(requests)
            except InfeasiblePlanError:
                # Random pin sets/capacities can be genuinely impossible.
                continue

            report = TickInterpreter(planner, requests).run_all(plan)
            self.assertTrue(report.ok, f"trial {trial}: {report.violations}")
            self.assertTrue(
                all(s.used_bytes <= capacity for s in report.trace),
                f"trial {trial}: capacity exceeded",
            )
            self.assertEqual(
                plan.writeback_bytes,
                sum(w.byte_count for w in plan.writebacks),
            )

            try:
                greedy = access_distance_greedy_plan(planner, requests)
            except InfeasiblePlanError:
                # The deliberately lazy baseline can paint itself into a
                # pin/reservation corner that the exhaustive planner avoids by
                # proactive eviction.  Such inputs still validate the optimal
                # plan above; timing comparison applies when the baseline runs.
                continue
            greedy_report = TickInterpreter(planner, requests).run_all(greedy)
            self.assertTrue(greedy_report.ok, f"trial {trial}: {greedy_report.violations}")
            self.assertGreaterEqual(
                greedy.makespan_ticks,
                plan.makespan_ticks,
                f"trial {trial}: greedy beat exhaustive planner",
            )
            if greedy.makespan_ticks == plan.makespan_ticks:
                self.assertLessEqual(plan.writeback_bytes, greedy.writeback_bytes)


if __name__ == "__main__":
    unittest.main()
