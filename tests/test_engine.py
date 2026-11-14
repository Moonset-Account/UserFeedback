"""Unit tests for the remediation engine rules.

Each test carries a stable case_id (also referenced from README's mapping).
Run via: /usr/local/bin/python3 -m unittest discover -s tests -v
"""

import unittest
from datetime import datetime, timezone

from remediation import Action, Clock, PlanReason, PlanStatus, RemediationEngine

UTC = timezone.utc


def ts(hour, day=14):
    return datetime(2026, 11, day, hour, 0, 0, tzinfo=UTC)


class EngineRuleTests(unittest.TestCase):
    def make_engine(self, start=None, budgets=None):
        return RemediationEngine(
            Clock(start or ts(9)),
            budgets if budgets is not None else {"team-a": 10, "team-b": 6},
        )

    # TC-01 non-production group inside budget locks points and becomes pending
    def test_tc01_non_prod_locks_budget(self):
        engine = self.make_engine()
        plan = engine.report("A1", "team-a", "staging", "high", 6, None)
        self.assertEqual(plan.status, PlanStatus.PENDING_EXECUTION)
        self.assertEqual(plan.locked_points, 6)
        self.assertEqual(plan.reason, PlanReason.LOCKED_ATOMIC_GROUP)
        self.assertEqual(engine.ledger.remaining("team-a", "2026-11-14"), 4)

    # TC-02 demand over remaining budget locks nothing
    def test_tc02_budget_exceeded_locks_nothing(self):
        engine = self.make_engine()
        engine.report("A1", "team-a", "staging", "high", 6, None)
        plan = engine.report("A2", "team-a", "staging", "medium", 5, None)
        self.assertEqual(plan.status, PlanStatus.BUDGET_EXCEEDED)
        self.assertEqual(plan.locked_points, 0)
        self.assertEqual(plan.reason, PlanReason.BUDGET_INSUFFICIENT)
        # the failed group must not consume any quota
        self.assertEqual(engine.ledger.remaining("team-a", "2026-11-14"), 4)

    # TC-03 exact-fit demand consumes the full daily budget
    def test_tc03_exact_fit_uses_full_budget(self):
        engine = self.make_engine()
        plan = engine.report("A1", "team-b", "dev", "low", 6, None)
        self.assertEqual(plan.status, PlanStatus.PENDING_EXECUTION)
        self.assertEqual(engine.ledger.remaining("team-b", "2026-11-14"), 0)

    # TC-04 production resources only enter review, no budget lock
    def test_tc04_production_goes_to_review_only(self):
        engine = self.make_engine()
        plan = engine.report("A1", "team-a", "production", "critical", 99, None)
        self.assertEqual(plan.status, PlanStatus.PENDING_REVIEW)
        self.assertEqual(plan.locked_points, 0)
        self.assertEqual(plan.reason, PlanReason.PRODUCTION_REVIEW_ONLY)
        self.assertEqual(engine.ledger.remaining("team-a", "2026-11-14"), 10)

    # TC-05 manual exemption window (at report time) blocks locking
    def test_tc05_exemption_window_blocks_lock(self):
        engine = self.make_engine()
        plan = engine.report(
            "A1", "team-a", "dev", "low", 3, (ts(8), ts(12))
        )
        self.assertEqual(plan.status, PlanStatus.EXEMPTED)
        self.assertEqual(plan.locked_points, 0)
        self.assertEqual(plan.reason, PlanReason.MANUAL_EXEMPTION_WINDOW)
        self.assertEqual(engine.ledger.remaining("team-a", "2026-11-14"), 10)

    # TC-06 window boundaries are inclusive
    def test_tc06_exemption_window_boundaries_inclusive(self):
        engine = self.make_engine(start=ts(12))
        plan = engine.report("A1", "team-a", "dev", "low", 3, (ts(8), ts(12)))
        self.assertEqual(plan.status, PlanStatus.EXEMPTED)
        engine.clock.set_time(ts(12, 15))
        plan2 = engine.report("A2", "team-a", "dev", "low", 3, (ts(8), ts(12)))
        self.assertEqual(plan2.status, PlanStatus.PENDING_EXECUTION)

    # TC-07 one exempt action in the atomic group exempts the whole group
    def test_tc07_atomic_group_one_exempt_blocks_all(self):
        engine = self.make_engine()
        actions = [
            Action("r1", "team-a", "dev", 2, None),
            Action("r2", "team-a", "dev", 3, (ts(8), ts(12))),
        ]
        plan = engine.report("A1", "team-a", "dev", "high", 0, None, actions=actions)
        self.assertEqual(plan.status, PlanStatus.EXEMPTED)
        self.assertEqual(plan.locked_points, 0)
        self.assertEqual(engine.ledger.remaining("team-a", "2026-11-14"), 10)

    # TC-08 one production action sends the whole group to review
    def test_tc08_atomic_group_one_production_review_all(self):
        engine = self.make_engine()
        actions = [
            Action("r1", "team-a", "dev", 2, None),
            Action("r2", "team-a", "production", 3, None),
        ]
        plan = engine.report("A1", "team-a", "dev", "high", 0, None, actions=actions)
        self.assertEqual(plan.status, PlanStatus.PENDING_REVIEW)
        self.assertEqual(plan.locked_points, 0)
        self.assertEqual(engine.ledger.remaining("team-a", "2026-11-14"), 10)

    # TC-09 group exceeding budget locks nothing for any member
    def test_tc09_atomic_group_over_budget_locks_nothing(self):
        engine = self.make_engine()
        actions = [
            Action("r1", "team-a", "dev", 6, None),
            Action("r2", "team-a", "dev", 5, None),
        ]
        plan = engine.report("A1", "team-a", "dev", "high", 0, None, actions=actions)
        self.assertEqual(plan.status, PlanStatus.BUDGET_EXCEEDED)
        self.assertEqual(plan.locked_points, 0)
        self.assertEqual(engine.ledger.remaining("team-a", "2026-11-14"), 10)

    # TC-10 affordable atomic group locks all actions together
    def test_tc10_atomic_group_locks_together(self):
        engine = self.make_engine()
        actions = [
            Action("r1", "team-a", "dev", 4, None),
            Action("r2", "team-a", "dev", 3, None),
        ]
        plan = engine.report("A1", "team-a", "dev", "high", 0, None, actions=actions)
        self.assertEqual(plan.status, PlanStatus.PENDING_EXECUTION)
        self.assertEqual(plan.locked_points, 7)
        self.assertEqual(plan.team_locks, {"team-a": 7})
        self.assertEqual(engine.ledger.remaining("team-a", "2026-11-14"), 3)

    # TC-11 cancellation on the same day releases the unused quota
    def test_tc11_cancel_same_day_releases_quota(self):
        engine = self.make_engine()
        engine.report("A1", "team-a", "staging", "high", 6, None)
        plan = engine.cancel("A1")
        self.assertEqual(plan.status, PlanStatus.CANCELLED)
        self.assertEqual(plan.released_points, 6)
        self.assertEqual(plan.locked_points, 0)
        self.assertEqual(plan.reason, PlanReason.CANCELLED_RELEASED)
        self.assertEqual(engine.ledger.remaining("team-a", "2026-11-14"), 10)
        # released quota is reusable
        plan2 = engine.report("A2", "team-a", "staging", "low", 10, None)
        self.assertEqual(plan2.status, PlanStatus.PENDING_EXECUTION)

    # TC-12 cancelling a plan that never locked anything holds no quota
    def test_tc12_cancel_unlocked_plan_nothing_held(self):
        engine = self.make_engine()
        engine.report("A1", "team-a", "production", "high", 9, None)
        plan = engine.cancel("A1")
        self.assertEqual(plan.status, PlanStatus.CANCELLED)
        self.assertEqual(plan.released_points, 0)
        self.assertEqual(plan.reason, PlanReason.CANCELLED_NOTHING_HELD)

    # TC-13 duplicate report reuses the original plan and does not double-lock
    def test_tc13_duplicate_report_reuses_original_plan(self):
        engine = self.make_engine()
        first = engine.report("A1", "team-a", "staging", "high", 6, None)
        second = engine.report("A1", "team-a", "staging", "high", 6, None)
        self.assertIs(first, second)
        self.assertTrue(second.reused)
        self.assertEqual(second.reason, PlanReason.DUPLICATE_REUSED)
        self.assertEqual(second.locked_points, 6)
        self.assertEqual(engine.ledger.remaining("team-a", "2026-11-14"), 4)

    # TC-14 budget resets on the next UTC day (controllable clock)
    def test_tc14_budget_resets_across_days(self):
        engine = self.make_engine()
        engine.report("A1", "team-a", "staging", "high", 10, None)
        self.assertEqual(engine.ledger.remaining("team-a", "2026-11-14"), 0)
        engine.clock.advance(hours=25)
        self.assertEqual(engine.clock.day_key(), "2026-11-15")
        plan = engine.report("A2", "team-a", "staging", "high", 8, None)
        self.assertEqual(plan.status, PlanStatus.PENDING_EXECUTION)
        self.assertEqual(plan.locked_points, 8)
        self.assertEqual(engine.ledger.remaining("team-a", "2026-11-15"), 2)
        # yesterday's ledger stays closed at zero remaining
        self.assertEqual(engine.ledger.remaining("team-a", "2026-11-14"), 0)

    # TC-15 cross-day cancellation cannot restore yesterday's budget
    def test_tc15_cancel_cross_day_keeps_yesterday_closed(self):
        engine = self.make_engine()
        engine.report("A1", "team-a", "staging", "high", 6, None)
        engine.clock.advance(hours=25)
        plan = engine.cancel("A1")
        self.assertEqual(plan.status, PlanStatus.CANCELLED)
        self.assertEqual(plan.released_points, 0)
        self.assertEqual(plan.reason, PlanReason.CANCELLED_NOTHING_HELD)
        self.assertEqual(engine.ledger.remaining("team-a", "2026-11-14"), 4)
        self.assertEqual(engine.ledger.remaining("team-a", "2026-11-15"), 10)

    # TC-16 multi-team group checks every involved team's budget
    def test_tc16_multi_team_group_demand_per_team(self):
        engine = self.make_engine()
        actions = [
            Action("r1", "team-a", "dev", 8, None),
            Action("r2", "team-b", "dev", 5, None),
        ]
        plan = engine.report("A1", "team-a", "dev", "high", 0, None, actions=actions)
        self.assertEqual(plan.status, PlanStatus.PENDING_EXECUTION)
        self.assertEqual(plan.locked_points, 13)
        self.assertEqual(plan.team_locks, {"team-a": 8, "team-b": 5})
        self.assertEqual(engine.ledger.remaining("team-a", "2026-11-14"), 2)
        self.assertEqual(engine.ledger.remaining("team-b", "2026-11-14"), 1)

    # TC-17 one team short on budget blocks the whole multi-team group
    def test_tc17_multi_team_one_short_blocks_all(self):
        engine = self.make_engine()
        engine.report("A0", "team-b", "dev", "low", 2, None)  # team-b left 4
        actions = [
            Action("r1", "team-a", "dev", 2, None),
            Action("r2", "team-b", "dev", 5, None),
        ]
        plan = engine.report("A1", "team-a", "dev", "high", 0, None, actions=actions)
        self.assertEqual(plan.status, PlanStatus.BUDGET_EXCEEDED)
        self.assertEqual(plan.locked_points, 0)
        self.assertEqual(engine.ledger.remaining("team-a", "2026-11-14"), 10)
        self.assertEqual(engine.ledger.remaining("team-b", "2026-11-14"), 4)

    # TC-18 zero-point actions lock zero but still produce executable plans
    def test_tc18_zero_impact_plan(self):
        engine = self.make_engine()
        plan = engine.report("A1", "team-a", "dev", "info", 0, None)
        self.assertEqual(plan.status, PlanStatus.PENDING_EXECUTION)
        self.assertEqual(plan.locked_points, 0)
        self.assertEqual(engine.ledger.remaining("team-a", "2026-11-14"), 10)

    # TC-19 cancelling twice is idempotent
    def test_tc19_double_cancel_is_idempotent(self):
        engine = self.make_engine()
        engine.report("A1", "team-a", "staging", "high", 4, None)
        first = engine.cancel("A1")
        second = engine.cancel("A1")
        self.assertIs(first, second)
        self.assertEqual(second.status, PlanStatus.CANCELLED)
        self.assertEqual(second.reason, PlanReason.ALREADY_CANCELLED)
        self.assertEqual(second.released_points, 4)
        self.assertEqual(engine.ledger.remaining("team-a", "2026-11-14"), 10)

    # TC-20 invalid inputs are rejected
    def test_tc20_input_validation(self):
        engine = self.make_engine()
        with self.assertRaises(ValueError):
            engine.report("", "team-a", "dev", "low", 1, None)
        with self.assertRaises(ValueError):
            engine.report("X", "team-a", "dev", "low", -1, None)
        with self.assertRaises(ValueError):
            engine.report("X", "team-a", "dev", "low", True, None)
        with self.assertRaises(ValueError):
            Action("r", "team-a", "dev", 1, (ts(12), ts(8)))
        with self.assertRaises(ValueError):
            RemediationEngine(Clock(), {"team-a": -1})
        with self.assertRaises(KeyError):
            engine.cancel("never-reported")
        with self.assertRaises(ValueError):
            engine.report("X", "ghost-team", "dev", "low", 1, None)

    # TC-21 clock cannot move backwards
    def test_tc21_clock_rewind_rejected(self):
        clock = Clock(ts(10))
        with self.assertRaises(ValueError):
            clock.set_time(ts(9))

    # TC-22 priority: production review beats exemption and budget
    def test_tc22_production_priority_over_exemption(self):
        engine = self.make_engine()
        plan = engine.report(
            "A1", "team-a", "production", "high", 99, (ts(8), ts(12))
        )
        self.assertEqual(plan.status, PlanStatus.PENDING_REVIEW)
        self.assertEqual(plan.reason, PlanReason.PRODUCTION_REVIEW_ONLY)

    # TC-23 budget snapshot exposes per-day locked and remaining quota
    def test_tc23_budget_snapshot(self):
        engine = self.make_engine()
        engine.report("A1", "team-a", "staging", "high", 6, None)
        engine.clock.advance(hours=25)
        engine.report("A2", "team-a", "staging", "low", 3, None)
        snap = engine.budget_snapshot()["team-a"]
        self.assertEqual(snap["daily_budget"], 10)
        self.assertEqual(snap["days"]["2026-11-14"]["locked"], 6)
        self.assertEqual(snap["days"]["2026-11-14"]["remaining"], 4)
        self.assertEqual(snap["days"]["2026-11-15"]["locked"], 3)
        self.assertEqual(snap["days"]["2026-11-15"]["remaining"], 7)


if __name__ == "__main__":
    unittest.main(verbosity=2)
