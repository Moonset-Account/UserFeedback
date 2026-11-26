import unittest

from safe_dump_planner import (
    DiagnosticTarget,
    DomainPolicy,
    DumpRequest,
    PageSpec,
    VirtualAlias,
    plan_safe_dump,
)


def allow_all_request(targets, budget=4096, page_size=512, objective="lexicographic"):
    return DumpRequest(
        pages=[PageSpec("p", size=page_size)],
        policies=[DomainPolicy.full("d")],
        aliases=[VirtualAlias("alias-p", "p", domain="d")],
        targets=targets,
        budget_bytes=budget,
        objective=objective,
    )


class BudgetSelectionTests(unittest.TestCase):
    def test_higher_priority_target_wins_over_more_cheap_low_priority_targets(self):
        request = allow_all_request(
            [
                DiagnosticTarget("high", priority=10, page_id="p", ranges=[(0, 100)]),
                DiagnosticTarget("low-1", priority=1, page_id="p", ranges=[(100, 160)]),
                DiagnosticTarget("low-2", priority=1, page_id="p", ranges=[(160, 220)]),
            ],
            budget=120,
        )
        plan = plan_safe_dump(request)
        statuses = {t.target_id: t.status for t in plan.targets}
        self.assertEqual(statuses["high"], "retained")
        self.assertEqual(statuses["low-1"], "budget_denied")
        self.assertEqual(statuses["low-2"], "budget_denied")
        self.assertEqual(plan.retained_bytes, 100)
        low = next(t for t in plan.targets if t.target_id == "low-1")
        self.assertEqual(low.marginal_bytes, 60)
        self.assertEqual(low.reasons[0].details["remaining_budget_bytes"], 20)

    def test_same_priority_maximizes_count_before_minimizing_bytes(self):
        request = allow_all_request(
            [
                DiagnosticTarget("one-wide", priority=5, page_id="p", ranges=[(0, 80)]),
                DiagnosticTarget("pair-a", priority=5, page_id="p", ranges=[(100, 140)]),
                DiagnosticTarget("pair-b", priority=5, page_id="p", ranges=[(140, 180)]),
            ],
            budget=80,
        )
        plan = plan_safe_dump(request)
        supported = {t.target_id for t in plan.targets if t.supported}
        self.assertEqual(supported, {"pair-a", "pair-b"})
        self.assertEqual(plan.retained_bytes, 80)
        # Adjacent selected atoms are represented as one minimal fragment.
        pair_fragment = next(
            f for f in plan.fragments if f.start == 100 and f.end == 180
        )
        self.assertEqual(pair_fragment.length, 80)

    def test_shared_fragments_are_counted_as_union_not_sum(self):
        request = allow_all_request(
            [
                DiagnosticTarget("a", priority=1, page_id="p", ranges=[(0, 64)]),
                DiagnosticTarget("b", priority=1, page_id="p", ranges=[(32, 96)]),
            ],
            budget=96,
        )
        plan = plan_safe_dump(request)
        self.assertEqual({t.target_id for t in plan.targets if t.supported}, {"a", "b"})
        self.assertEqual(plan.retained_bytes, 96)
        self.assertEqual(len(plan.fragments), 1)
        self.assertEqual(plan.fragments[0].start, 0)
        self.assertEqual(plan.fragments[0].end, 96)

    def test_exact_budget_boundary(self):
        targets = [DiagnosticTarget("t", page_id="p", ranges=[(16, 80)])]
        ok = plan_safe_dump(allow_all_request(targets, budget=64))
        self.assertEqual(ok.targets[0].status, "retained")
        self.assertTrue(ok.feasible)

        too_small = plan_safe_dump(allow_all_request(targets, budget=63))
        self.assertEqual(too_small.targets[0].status, "budget_denied")
        self.assertEqual(too_small.retained_bytes, 0)
        reason = too_small.targets[0].reasons[0]
        self.assertEqual(reason.code, "BUDGET_EXCEEDED")
        self.assertEqual(reason.details["deficit_bytes"], 1)

    def test_count_objective(self):
        request = allow_all_request(
            [
                DiagnosticTarget("expensive", priority=100, page_id="p", ranges=[(0, 80)]),
                DiagnosticTarget("cheap-1", priority=0, page_id="p", ranges=[(100, 140)]),
                DiagnosticTarget("cheap-2", priority=0, page_id="p", ranges=[(140, 180)]),
            ],
            budget=80,
            objective="count",
        )
        plan = plan_safe_dump(request)
        self.assertEqual(
            {t.target_id for t in plan.targets if t.supported},
            {"cheap-1", "cheap-2"},
        )


if __name__ == "__main__":
    unittest.main()
