import random
import unittest

from safe_dump_planner import (
    DiagnosticTarget,
    DomainPolicy,
    DumpRequest,
    PageSpec,
    VirtualAlias,
    plan_safe_dump,
)
from safe_dump_planner.geometry import normalize_ranges


class ExactBudgetSelectionPropertyTests(unittest.TestCase):
    def test_planner_matches_brute_force_for_random_interleaved_ranges(self):
        rng = random.Random(20261127)
        for case in range(120):
            n = rng.randint(1, 9)
            page_size = 128
            targets = []
            intervals = []
            for i in range(n):
                start = rng.randrange(0, page_size - 8)
                end = min(page_size, start + rng.randint(1, 40))
                intervals.append((start, end))
                targets.append(
                    DiagnosticTarget(
                        f"t{i}",
                        priority=rng.choice([0, 1, 2, 5]),
                        page_id="p",
                        ranges=[(start, end)],
                    )
                )
            budget = rng.choice([0, 16, 32, 48, 64, 96, 128, None])
            request = DumpRequest(
                pages=[PageSpec("p", size=page_size)],
                aliases=[VirtualAlias("a", "p", domain="d")],
                policies=[DomainPolicy.full("d", addresses=True)],
                targets=targets,
                budget_bytes=budget,
            )
            plan = plan_safe_dump(request)
            self.assertTrue(plan.optimization["exact"])

            levels = sorted({t.priority for t in targets}, reverse=True)

            def objective_for_covered(covered):
                return tuple(
                    sum(1 for i in covered if targets[i].priority == level)
                    for level in levels
                )

            best_obj = None
            best_cost = None
            limit = 1 << n
            for mask in range(limit):
                chosen_ranges = [
                    intervals[i] for i in range(n) if mask & (1 << i)
                ]
                union = normalize_ranges(chosen_ranges)
                cost = sum(r.length for r in union)
                if budget is not None and cost > budget:
                    continue
                covered = set()
                for i, (start, end) in enumerate(intervals):
                    if all(
                        any(r.start <= start_i and end_i <= r.end for r in union)
                        for start_i, end_i in [(start, end)]
                    ):
                        covered.add(i)
                obj = objective_for_covered(covered)
                if best_obj is None or obj > best_obj or (
                    obj == best_obj and cost < best_cost
                ):
                    best_obj = obj
                    best_cost = cost

            planned_counts = tuple(
                sum(
                    1
                    for t in plan.targets
                    if t.supported and t.priority == level
                )
                for level in levels
            )
            self.assertEqual(
                planned_counts,
                best_obj,
                msg=f"case {case}: objective mismatch intervals={intervals} budget={budget}",
            )
            self.assertEqual(
                plan.retained_bytes,
                best_cost,
                msg=f"case {case}: cost mismatch intervals={intervals} budget={budget}",
            )


if __name__ == "__main__":
    unittest.main()
