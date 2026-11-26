"""Tests for the audit retention planner.

Run from the workspace root with::

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import itertools
import unittest

from retention import (
    CaseSpec,
    EvidenceLink,
    EvidenceRequirement,
    LogRecord,
    RetentionCapacityError,
    RetentionInputError,
    RetentionRequest,
    build_plan,
)


def make_request(logs, cases, links, budget, **kwargs):
    return RetentionRequest(
        logs=logs,
        cases=cases,
        links=links,
        budget=budget,
        **kwargs,
    )


def basic_triplet(prefix="c1", tier=1):
    """initiation a, approval b, effect c, all separate sources."""
    logs = [
        LogRecord("a", "sA", 10, 10),
        LogRecord("b", "sB", 20, 10),
        LogRecord("c", "sC", 30, 10),
    ]
    cases = [CaseSpec(prefix, tier)]
    links = [
        EvidenceLink(prefix, "initiation", "a"),
        EvidenceLink(prefix, "approval", "b"),
        EvidenceLink(prefix, "effect", "c"),
    ]
    return logs, cases, links


class BasicSatisfactionTests(unittest.TestCase):
    def test_default_three_item_combination(self):
        logs, cases, links = basic_triplet()
        plan = build_plan(make_request(logs, cases, links, 30))

        self.assertEqual(plan.satisfied_case_ids, ("c1",))
        self.assertEqual(plan.total_size, 30)
        self.assertEqual(plan.remaining_budget, 0)
        self.assertTrue(plan.optimal)

        choices = {c.role: c for c in plan.cases[0].evidence}
        self.assertEqual(set(choices), {"initiation", "approval", "effect"})
        self.assertEqual(choices["initiation"].log_id, "a")
        self.assertEqual(choices["approval"].log_id, "b")
        self.assertEqual(choices["effect"].log_id, "c")
        self.assertEqual(choices["initiation"].source_id, "sA")

    def test_returns_retained_logs_for_archive_task(self):
        logs, cases, links = basic_triplet()
        plan = build_plan(make_request(logs, cases, links, 30))

        self.assertEqual([r.log_id for r in plan.logs], ["a", "b", "c"])
        self.assertEqual([s.source_id for s in plan.sources], ["sA", "sB", "sC"])
        self.assertEqual(plan.droppable_copies, ())
        for log in plan.logs:
            self.assertEqual(log.size, 10)

    def test_zero_cases_yields_empty_plan(self):
        plan = build_plan(RetentionRequest(logs=[], cases=[], links=[], budget=100))
        self.assertEqual(plan.satisfied_case_ids, ())
        self.assertEqual(plan.total_size, 0)
        self.assertEqual(plan.sources, ())
        self.assertEqual(plan.logs, ())
        self.assertEqual(plan.unmet_evidence, ())
        self.assertTrue(plan.optimal)

    def test_satisfied_by_tier_reports_zero_tiers(self):
        logs, cases, links = basic_triplet()
        plan = build_plan(make_request(logs, cases, links, 30))
        self.assertEqual(plan.satisfied_by_tier, ((1, 1),))


class IndependenceTests(unittest.TestCase):
    def test_approval_copied_from_initiation_source_is_rejected(self):
        logs = [
            LogRecord("a", "sA", 10, 10),
            LogRecord("a_copy", "sA", 10, 10, copied_from="a"),
            LogRecord("c", "sC", 30, 10),
        ]
        cases = [CaseSpec("c1", 1)]
        links = [
            EvidenceLink("c1", "initiation", "a"),
            EvidenceLink("c1", "approval", "a_copy"),
            EvidenceLink("c1", "effect", "c"),
        ]
        plan = build_plan(make_request(logs, cases, links, 100))

        self.assertEqual(plan.satisfied_case_ids, ())
        reasons = {u.role: u.reason for u in plan.unmet_evidence}
        self.assertEqual(set(reasons), {"initiation", "approval", "effect"})
        self.assertTrue(all(r == "no_feasible_combination" for r in reasons.values()))

    def test_receipt_copy_from_other_source_cannot_be_laundered(self):
        # A receipt that claims a different source than its original is invalid.
        logs = [
            LogRecord("a", "sA", 10, 10),
            LogRecord("a_copy", "sB", 10, 10, copied_from="a"),
        ]
        with self.assertRaises(RetentionInputError):
            build_plan(
                make_request(
                    logs,
                    [CaseSpec("c1", 1)],
                    [
                        EvidenceLink("c1", "initiation", "a"),
                        EvidenceLink("c1", "approval", "a_copy"),
                        EvidenceLink("c1", "effect", "a"),
                    ],
                    100,
                )
            )

    def test_independent_sources_satisfy_case(self):
        logs = [
            LogRecord("a", "sA", 10, 10),
            LogRecord("b", "sB", 20, 10),
            LogRecord("c", "sC", 30, 10),
        ]
        plan = build_plan(
            make_request(
                logs,
                [CaseSpec("c1", 1)],
                [
                    EvidenceLink("c1", "initiation", "a"),
                    EvidenceLink("c1", "approval", "b"),
                    EvidenceLink("c1", "effect", "c"),
                ],
                30,
            )
        )
        self.assertEqual(plan.satisfied_case_ids, ("c1",))


class OrderingTests(unittest.TestCase):
    def test_approval_must_not_precede_initiation(self):
        logs = [
            LogRecord("early", "sEarly", 5, 10),
            LogRecord("init", "sInit", 10, 10),
            LogRecord("effect", "sEffect", 30, 10),
        ]
        plan = build_plan(
            make_request(
                logs,
                [CaseSpec("c1", 1)],
                [
                    EvidenceLink("c1", "initiation", "init"),
                    EvidenceLink("c1", "approval", "early"),
                    EvidenceLink("c1", "effect", "effect"),
                ],
                100,
            )
        )
        self.assertEqual(plan.satisfied_case_ids, ())

    def test_effect_must_not_precede_approval(self):
        logs = [
            LogRecord("init", "sInit", 10, 10),
            LogRecord("approval", "sApproval", 20, 10),
            LogRecord("effect", "sEffect", 15, 10),
        ]
        plan = build_plan(
            make_request(
                logs,
                [CaseSpec("c1", 1)],
                [
                    EvidenceLink("c1", "initiation", "init"),
                    EvidenceLink("c1", "approval", "approval"),
                    EvidenceLink("c1", "effect", "effect"),
                ],
                100,
            )
        )
        self.assertEqual(plan.satisfied_case_ids, ())

    def test_equal_timestamps_allowed_for_non_strict_after(self):
        logs = [
            LogRecord("init", "sInit", 10, 10),
            LogRecord("approval", "sApproval", 10, 10),
            LogRecord("effect", "sEffect", 10, 10),
        ]
        plan = build_plan(
            make_request(
                logs,
                [CaseSpec("c1", 1)],
                [
                    EvidenceLink("c1", "initiation", "init"),
                    EvidenceLink("c1", "approval", "approval"),
                    EvidenceLink("c1", "effect", "effect"),
                ],
                30,
            )
        )
        self.assertEqual(plan.satisfied_case_ids, ("c1",))

    def test_strict_after_rejects_equal_timestamps(self):
        requirements = (
            EvidenceRequirement("initiation"),
            EvidenceRequirement("approval", after="initiation", strict_after=True),
        )
        logs = [
            LogRecord("init", "sInit", 10, 10),
            LogRecord("approval", "sApproval", 10, 10),
        ]
        plan = build_plan(
            make_request(
                logs,
                [CaseSpec("c1", 1, requirements=requirements)],
                [
                    EvidenceLink("c1", "initiation", "init"),
                    EvidenceLink("c1", "approval", "approval"),
                ],
                100,
            )
        )
        self.assertEqual(plan.satisfied_case_ids, ())

    def test_requirement_cycle_is_rejected(self):
        requirements = (
            EvidenceRequirement("a", after="b"),
            EvidenceRequirement("b", after="a"),
        )
        logs = [LogRecord("x", "sX", 1, 1)]
        with self.assertRaises(RetentionInputError):
            build_plan(
                make_request(
                    logs,
                    [CaseSpec("c1", 1, requirements=requirements)],
                    [EvidenceLink("c1", "a", "x")],
                    10,
                )
            )


class CopyAccountingTests(unittest.TestCase):
    def test_copies_of_one_source_are_charged_once(self):
        # Initiation and effect both read from source sA; only one charge.
        logs = [
            LogRecord("a1", "sA", 10, 100),
            LogRecord("a2", "sA", 30, 100),
            LogRecord("b", "sB", 20, 50),
        ]
        cases = [CaseSpec("c1", 1)]
        links = [
            EvidenceLink("c1", "initiation", "a1"),
            EvidenceLink("c1", "approval", "b"),
            EvidenceLink("c1", "effect", "a2"),
        ]
        plan = build_plan(make_request(logs, cases, links, 150))
        self.assertEqual(plan.satisfied_case_ids, ("c1",))
        self.assertEqual(plan.total_size, 150)
        self.assertEqual({s.source_id for s in plan.sources}, {"sA", "sB"})

    def test_budget_counts_source_once(self):
        # With per-log charging this would need 200; source charging needs 150.
        logs = [
            LogRecord("a1", "sA", 10, 100),
            LogRecord("a2", "sA", 30, 100),
            LogRecord("b", "sB", 20, 50),
        ]
        cases = [CaseSpec("c1", 1)]
        links = [
            EvidenceLink("c1", "initiation", "a1"),
            EvidenceLink("c1", "approval", "b"),
            EvidenceLink("c1", "effect", "a2"),
        ]
        plan = build_plan(make_request(logs, cases, links, 149))
        self.assertEqual(plan.satisfied_case_ids, ())

    def test_unused_receipts_are_reported_as_droppable(self):
        logs = [
            LogRecord("a", "sA", 10, 10),
            LogRecord("a_copy", "sA", 10, 10, copied_from="a"),
            LogRecord("b", "sB", 20, 10),
            LogRecord("c", "sC", 30, 10),
        ]
        cases = [CaseSpec("c1", 1)]
        links = [
            EvidenceLink("c1", "initiation", "a"),
            EvidenceLink("c1", "approval", "b"),
            EvidenceLink("c1", "effect", "c"),
        ]
        plan = build_plan(make_request(logs, cases, links, 30))
        self.assertEqual([r.log_id for r in plan.droppable_copies], ["a_copy"])
        source = {s.source_id: s for s in plan.sources}["sA"]
        self.assertEqual(source.kept_log_ids, ("a",))
        self.assertEqual(source.dropped_copy_ids, ("a_copy",))

    def test_choosing_a_receipt_keeps_its_original(self):
        logs = [
            LogRecord("a", "sA", 10, 10),
            LogRecord("a_copy", "sA", 10, 10, copied_from="a"),
            LogRecord("b", "sB", 20, 10),
            LogRecord("c", "sC", 30, 10),
        ]
        cases = [CaseSpec("c1", 1)]
        links = [
            EvidenceLink("c1", "initiation", "a_copy"),
            EvidenceLink("c1", "approval", "b"),
            EvidenceLink("c1", "effect", "c"),
        ]
        plan = build_plan(make_request(logs, cases, links, 30))
        self.assertEqual(plan.satisfied_case_ids, ("c1",))
        self.assertEqual({r.log_id for r in plan.logs}, {"a", "a_copy", "b", "c"})


class SharingAcrossCasesTests(unittest.TestCase):
    def test_one_source_can_serve_shared_roles_in_two_cases(self):
        logs = [
            LogRecord("a", "sA", 10, 100),
            LogRecord("b", "sB", 20, 40),
            LogRecord("c", "sC", 30, 40),
            LogRecord("d", "sD", 40, 10),
        ]
        cases = [CaseSpec("c1", 1), CaseSpec("c2", 1)]
        links = [
            EvidenceLink("c1", "initiation", "a"),
            EvidenceLink("c1", "approval", "b"),
            EvidenceLink("c1", "effect", "c"),
            EvidenceLink("c2", "initiation", "a"),
            EvidenceLink("c2", "approval", "b"),
            EvidenceLink("c2", "effect", "d"),
        ]
        plan = build_plan(make_request(logs, cases, links, 190))
        self.assertEqual(set(plan.satisfied_case_ids), {"c1", "c2"})
        self.assertEqual(plan.total_size, 190)
        self.assertEqual(
            sorted(s.source_id for s in plan.sources), ["sA", "sB", "sC", "sD"]
        )

    def test_shared_source_saves_budget_versus_separate(self):
        # Two cases can only both fit because the source is charged once.
        logs = [
            LogRecord("a", "sA", 10, 60),
            LogRecord("b", "sB", 20, 40),
            LogRecord("c", "sC", 30, 30),
            LogRecord("d", "sD", 40, 30),
        ]
        cases = [CaseSpec("c1", 1), CaseSpec("c2", 1)]
        links = [
            EvidenceLink("c1", "initiation", "a"),
            EvidenceLink("c1", "approval", "b"),
            EvidenceLink("c1", "effect", "c"),
            EvidenceLink("c2", "initiation", "a"),
            EvidenceLink("c2", "approval", "b"),
            EvidenceLink("c2", "effect", "d"),
        ]
        # Unique sources cost 60+40+30+30 = 160; budget 160 fits both.
        plan = build_plan(make_request(logs, cases, links, 160))
        self.assertEqual(set(plan.satisfied_case_ids), {"c1", "c2"})
        self.assertEqual(plan.total_size, 160)

        plan = build_plan(make_request(logs, cases, links, 159))
        self.assertEqual(len(plan.satisfied_case_ids), 1)


class BudgetOptimisationTests(unittest.TestCase):
    def _two_independent_cases(self):
        logs = [
            LogRecord("a1", "sA1", 10, 10),
            LogRecord("b1", "sB1", 20, 10),
            LogRecord("c1", "sC1", 30, 10),
            LogRecord("a2", "sA2", 40, 10),
            LogRecord("b2", "sB2", 50, 10),
            LogRecord("c2", "sC2", 60, 10),
        ]
        cases = [CaseSpec("c1", 1), CaseSpec("c2", 1)]
        links = [
            EvidenceLink("c1", "initiation", "a1"),
            EvidenceLink("c1", "approval", "b1"),
            EvidenceLink("c1", "effect", "c1"),
            EvidenceLink("c2", "initiation", "a2"),
            EvidenceLink("c2", "approval", "b2"),
            EvidenceLink("c2", "effect", "c2"),
        ]
        return logs, cases, links

    def test_maximises_satisfied_case_count(self):
        logs, cases, links = self._two_independent_cases()
        plan = build_plan(make_request(logs, cases, links, 60))
        self.assertEqual(set(plan.satisfied_case_ids), {"c1", "c2"})
        self.assertEqual(plan.total_size, 60)

    def test_partial_budget_drops_low_priority_case(self):
        logs, cases, links = self._two_independent_cases()
        plan = build_plan(make_request(logs, cases, links, 30))
        self.assertEqual(len(plan.satisfied_case_ids), 1)

    def test_higher_tier_wins_even_with_fewer_cases(self):
        # Tier 2 case needs 100; two tier-1 cases need 10 each (shared 20).
        logs = [
            LogRecord("h1", "sH1", 10, 40),
            LogRecord("h2", "sH2", 20, 30),
            LogRecord("h3", "sH3", 30, 30),
            LogRecord("l1", "sL1", 40, 10),
            LogRecord("l2", "sL2", 50, 10),
            LogRecord("l3", "sL3", 60, 10),
            LogRecord("l4", "sL4", 70, 10),
        ]
        cases = [CaseSpec("high", 2), CaseSpec("low1", 1), CaseSpec("low2", 1)]
        links = [
            EvidenceLink("high", "initiation", "h1"),
            EvidenceLink("high", "approval", "h2"),
            EvidenceLink("high", "effect", "h3"),
            EvidenceLink("low1", "initiation", "l1"),
            EvidenceLink("low1", "approval", "l2"),
            EvidenceLink("low1", "effect", "l3"),
            EvidenceLink("low2", "initiation", "l1"),
            EvidenceLink("low2", "approval", "l2"),
            EvidenceLink("low2", "effect", "l4"),
        ]
        plan = build_plan(make_request(logs, cases, links, 100))
        self.assertEqual(plan.satisfied_case_ids, ("high",))
        self.assertEqual(plan.satisfied_by_tier, ((2, 1), (1, 0)))

    def test_tier_maximisation_then_cheaper_tie_break(self):
        # Two cases in the same tier; only one fits either way. The cheaper
        # feasible combination must be retained.
        logs = [
            LogRecord("cheap_a", "sCA", 10, 5),
            LogRecord("cheap_b", "sCB", 20, 5),
            LogRecord("cheap_c", "sCC", 30, 5),
            LogRecord("dear_a", "sDA", 40, 30),
            LogRecord("dear_b", "sDB", 50, 30),
            LogRecord("dear_c", "sDC", 60, 30),
        ]
        cases = [CaseSpec("dear", 1), CaseSpec("cheap", 1)]
        links = [
            EvidenceLink("dear", "initiation", "dear_a"),
            EvidenceLink("dear", "approval", "dear_b"),
            EvidenceLink("dear", "effect", "dear_c"),
            EvidenceLink("cheap", "initiation", "cheap_a"),
            EvidenceLink("cheap", "approval", "cheap_b"),
            EvidenceLink("cheap", "effect", "cheap_c"),
        ]
        plan = build_plan(make_request(logs, cases, links, 40))
        self.assertEqual(plan.satisfied_case_ids, ("cheap",))
        self.assertEqual(plan.total_size, 15)

    def test_zero_budget_yields_nothing(self):
        logs, cases, links = basic_triplet()
        plan = build_plan(make_request(logs, cases, links, 0))
        self.assertEqual(plan.satisfied_case_ids, ())
        self.assertEqual(plan.total_size, 0)

    def test_zero_size_sources_fit_zero_budget(self):
        logs = [
            LogRecord("a", "sA", 10, 0),
            LogRecord("b", "sB", 20, 0),
            LogRecord("c", "sC", 30, 0),
        ]
        cases = [CaseSpec("c1", 1)]
        links = [
            EvidenceLink("c1", "initiation", "a"),
            EvidenceLink("c1", "approval", "b"),
            EvidenceLink("c1", "effect", "c"),
        ]
        plan = build_plan(make_request(logs, cases, links, 0))
        self.assertEqual(plan.satisfied_case_ids, ("c1",))


class UnmetEvidenceTests(unittest.TestCase):
    def test_no_candidate_reason(self):
        logs = [LogRecord("a", "sA", 10, 10)]
        cases = [CaseSpec("c1", 1)]
        links = [EvidenceLink("c1", "initiation", "a")]
        plan = build_plan(make_request(logs, cases, links, 100))
        reasons = {u.role: u.reason for u in plan.unmet_evidence}
        self.assertEqual(reasons["initiation"], "no_feasible_combination")
        self.assertEqual(reasons["approval"], "no_candidate")
        self.assertEqual(reasons["effect"], "no_candidate")

    def test_budget_insufficient_reason(self):
        logs, cases, links = basic_triplet()
        plan = build_plan(make_request(logs, cases, links, 29))
        self.assertEqual(plan.satisfied_case_ids, ())
        reasons = {u.role: u.reason for u in plan.unmet_evidence}
        self.assertTrue(all(r == "budget_insufficient" for r in reasons.values()))

    def test_unmet_reports_candidate_counts(self):
        logs, cases, links = basic_triplet()
        plan = build_plan(make_request(logs, cases, links, 29))
        for item in plan.unmet_evidence:
            self.assertEqual(item.candidate_count, 1)
            self.assertEqual(item.case_id, "c1")

    def test_satisfied_case_has_no_unmet_items(self):
        logs, cases, links = basic_triplet()
        plan = build_plan(make_request(logs, cases, links, 30))
        self.assertEqual(plan.cases[0].unmet, ())
        self.assertEqual(plan.unmet_evidence, ())

    def test_not_retained_reason_when_search_pruned(self):
        # c1 can be proven cheaply from sY/sZ/sW (cost 30) or from sX+sZ
        # (cost 40).  c2 can only be proven from sX+sZ (cost 40).  Optimal
        # shares sX+sZ: both fit in 40.  Capping the state table at one keeps
        # the cheaper c1 variant, so c2 no longer fits and the result is
        # explicitly marked non-optimal.
        logs = [
            LogRecord("x_i", "sX", 10, 30),
            LogRecord("x_a", "sZ", 20, 10),
            LogRecord("x_e", "sX", 30, 30),
            LogRecord("y_i", "sY", 10, 10),
            LogRecord("y_a", "sZ", 20, 10),
            LogRecord("y_e", "sW", 30, 10),
        ]
        cases = [CaseSpec("c1", 1), CaseSpec("c2", 1)]
        links = [
            EvidenceLink("c1", "initiation", "x_i"),
            EvidenceLink("c1", "approval", "x_a"),
            EvidenceLink("c1", "effect", "x_e"),
            EvidenceLink("c1", "initiation", "y_i"),
            EvidenceLink("c1", "approval", "y_a"),
            EvidenceLink("c1", "effect", "y_e"),
            EvidenceLink("c2", "initiation", "x_i"),
            EvidenceLink("c2", "approval", "x_a"),
            EvidenceLink("c2", "effect", "x_e"),
        ]
        optimal = build_plan(make_request(logs, cases, links, 40))
        self.assertTrue(optimal.optimal)
        self.assertEqual(set(optimal.satisfied_case_ids), {"c1", "c2"})
        self.assertEqual(optimal.total_size, 40)

        pruned = build_plan(
            make_request(logs, cases, links, 40, max_search_nodes=1)
        )
        self.assertFalse(pruned.optimal)
        self.assertEqual(pruned.satisfied_case_ids, ("c1",))
        unmet = [u for u in pruned.unmet_evidence if u.case_id == "c2"]
        self.assertEqual(len(unmet), 3)
        self.assertTrue(all(u.reason == "not_retained" for u in unmet))


class ValidationTests(unittest.TestCase):
    def test_duplicate_log_id(self):
        logs = [LogRecord("a", "sA", 1, 1), LogRecord("a", "sB", 2, 1)]
        with self.assertRaises(RetentionInputError):
            build_plan(RetentionRequest(logs, [], [], 10))

    def test_duplicate_case_id(self):
        cases = [CaseSpec("c1", 1), CaseSpec("c1", 2)]
        with self.assertRaises(RetentionInputError):
            build_plan(RetentionRequest([], cases, [], 10))

    def test_duplicate_role(self):
        requirements = (
            EvidenceRequirement("initiation"),
            EvidenceRequirement("initiation"),
        )
        with self.assertRaises(RetentionInputError):
            build_plan(
                RetentionRequest(
                    [], [CaseSpec("c1", 1, requirements=requirements)], [], 10
                )
            )

    def test_unknown_role_in_after(self):
        requirements = (EvidenceRequirement("approval", after="initiation"),)
        with self.assertRaises(RetentionInputError):
            build_plan(
                RetentionRequest(
                    [], [CaseSpec("c1", 1, requirements=requirements)], [], 10
                )
            )

    def test_strict_after_without_after(self):
        requirements = (EvidenceRequirement("approval", strict_after=True),)
        with self.assertRaises(RetentionInputError):
            build_plan(
                RetentionRequest(
                    [], [CaseSpec("c1", 1, requirements=requirements)], [], 10
                )
            )

    def test_unknown_case_in_link(self):
        with self.assertRaises(RetentionInputError):
            build_plan(
                RetentionRequest(
                    [LogRecord("a", "sA", 1, 1)],
                    [CaseSpec("c1", 1)],
                    [EvidenceLink("c9", "initiation", "a")],
                    10,
                )
            )

    def test_unknown_log_in_link(self):
        with self.assertRaises(RetentionInputError):
            build_plan(
                RetentionRequest(
                    [],
                    [CaseSpec("c1", 1)],
                    [EvidenceLink("c1", "initiation", "missing")],
                    10,
                )
            )

    def test_unknown_role_in_link(self):
        with self.assertRaises(RetentionInputError):
            build_plan(
                RetentionRequest(
                    [LogRecord("a", "sA", 1, 1)],
                    [CaseSpec("c1", 1)],
                    [EvidenceLink("c1", "bogus", "a")],
                    10,
                )
            )

    def test_negative_budget(self):
        with self.assertRaises(RetentionInputError):
            build_plan(RetentionRequest([], [], [], -1))

    def test_bool_budget_rejected(self):
        with self.assertRaises(RetentionInputError):
            build_plan(RetentionRequest([], [], [], True))

    def test_negative_size(self):
        with self.assertRaises(RetentionInputError):
            build_plan(
                RetentionRequest([LogRecord("a", "sA", 1, -5)], [], [], 10)
            )

    def test_non_finite_timestamp(self):
        with self.assertRaises(RetentionInputError):
            build_plan(
                RetentionRequest(
                    [LogRecord("a", "sA", float("nan"), 5)], [], [], 10
                )
            )

    def test_unknown_copied_from(self):
        with self.assertRaises(RetentionInputError):
            build_plan(
                RetentionRequest(
                    [LogRecord("a", "sA", 1, 5, copied_from="missing")], [], [], 10
                )
            )

    def test_copy_cycle(self):
        logs = [
            LogRecord("a", "sA", 1, 5, copied_from="b"),
            LogRecord("b", "sA", 2, 5, copied_from="a"),
        ]
        with self.assertRaises(RetentionInputError):
            build_plan(RetentionRequest(logs, [], [], 10))

    def test_requirement_unknown_role_in_distinct_source(self):
        requirements = (
            EvidenceRequirement("initiation"),
            EvidenceRequirement("approval", distinct_source_from="ghost"),
        )
        with self.assertRaises(RetentionInputError):
            build_plan(
                RetentionRequest(
                    [], [CaseSpec("c1", 1, requirements=requirements)], [], 10
                )
            )

    def test_case_without_requirements_rejected(self):
        with self.assertRaises(RetentionInputError):
            build_plan(
                RetentionRequest([], [CaseSpec("c1", 1, requirements=())], [], 10)
            )

    def test_source_cost_override_unknown_source(self):
        with self.assertRaises(RetentionInputError):
            build_plan(
                RetentionRequest(
                    [LogRecord("a", "sA", 1, 5)],
                    [],
                    [],
                    10,
                    source_costs={"sMissing": 3},
                )
            )

    def test_combo_node_limit(self):
        logs, cases, links = [], [], []
        for i in range(8):
            logs.append(LogRecord(f"l{i}", f"s{i}", i, 5))
        for i in range(8):
            for role in ("initiation", "approval", "effect"):
                links.append(EvidenceLink("c1", role, f"l{i}"))
        with self.assertRaises(RetentionCapacityError):
            build_plan(
                RetentionRequest(
                    logs, [CaseSpec("c1", 1)], links, 1000, max_combo_nodes=50
                )
            )


class SourceCostOverrideTests(unittest.TestCase):
    def test_explicit_source_cost_is_charged(self):
        logs = [
            LogRecord("a", "sA", 10, 100),
            LogRecord("b", "sB", 20, 10),
            LogRecord("c", "sC", 30, 10),
        ]
        cases = [CaseSpec("c1", 1)]
        links = [
            EvidenceLink("c1", "initiation", "a"),
            EvidenceLink("c1", "approval", "b"),
            EvidenceLink("c1", "effect", "c"),
        ]
        plan = build_plan(
            make_request(logs, cases, links, 40, source_costs={"sA": 20})
        )
        self.assertEqual(plan.satisfied_case_ids, ("c1",))
        self.assertEqual(plan.total_size, 40)

    def test_default_uses_largest_member_size(self):
        logs = [
            LogRecord("a1", "sA", 10, 100),
            LogRecord("a2", "sA", 11, 30),
            LogRecord("b", "sB", 20, 10),
            LogRecord("c", "sC", 30, 10),
        ]
        cases = [CaseSpec("c1", 1)]
        links = [
            EvidenceLink("c1", "initiation", "a1"),
            EvidenceLink("c1", "approval", "b"),
            EvidenceLink("c1", "effect", "c"),
        ]
        plan = build_plan(make_request(logs, cases, links, 120))
        self.assertEqual(plan.total_size, 120)


class DeterminismTests(unittest.TestCase):
    def test_same_input_same_plan(self):
        logs, cases, links = self._scenario()
        first = build_plan(make_request(logs, cases, links, 100))
        second = build_plan(make_request(logs, cases, links, 100))
        self.assertEqual(first, second)

    def test_link_order_does_not_matter(self):
        logs, cases, links = self._scenario()
        forward = build_plan(make_request(logs, cases, links, 100))
        reverse = build_plan(make_request(logs, cases, list(reversed(links)), 100))
        self.assertEqual(forward, reverse)

    def test_log_order_does_not_matter(self):
        logs, cases, links = self._scenario()
        forward = build_plan(make_request(logs, cases, links, 100))
        reverse = build_plan(make_request(list(reversed(logs)), cases, links, 100))
        self.assertEqual(forward, reverse)

    def _scenario(self):
        logs = [
            LogRecord("a", "sA", 10, 20),
            LogRecord("b", "sB", 20, 20),
            LogRecord("c", "sC", 30, 20),
            LogRecord("d", "sD", 40, 20),
            LogRecord("e", "sE", 50, 20),
        ]
        cases = [CaseSpec("c1", 2), CaseSpec("c2", 1)]
        links = [
            EvidenceLink("c1", "initiation", "a"),
            EvidenceLink("c1", "approval", "b"),
            EvidenceLink("c1", "effect", "c"),
            EvidenceLink("c2", "initiation", "b"),
            EvidenceLink("c2", "approval", "d"),
            EvidenceLink("c2", "effect", "e"),
        ]
        return logs, cases, links


# ---------------------------------------------------------------------------
# Independent brute-force oracle
# ---------------------------------------------------------------------------


def brute_force_plan(logs, cases, links, budget):
    """Exhaustive reference implementation used to validate the solver.

    Enumerates every combination of candidate logs per case directly, then
    every combination of per-case choices, and returns the lexicographic
    optimum as ``(per_case_choice, total_cost)`` using the same objective
    order: tier counts, then cost, then lexicographic choices.
    """
    log_by_id = {log.log_id: log for log in logs}
    source_size = {}
    for log in logs:
        source_size[log.source_id] = max(source_size.get(log.source_id, 0), log.size)

    requirements = {case.case_id: case.requirements for case in cases}
    candidates = {}
    for link in links:
        candidates.setdefault((link.case_id, link.role), []).append(link.log_id)

    def constraints_ok(case, assignment):
        for req in case.requirements:
            log = log_by_id[assignment[req.role]]
            if req.after is not None:
                ref = log_by_id[assignment[req.after]]
                if req.strict_after:
                    if not log.timestamp > ref.timestamp:
                        return False
                elif not log.timestamp >= ref.timestamp:
                    return False
            if req.distinct_source_from is not None:
                ref = log_by_id[assignment[req.distinct_source_from]]
                if log.source_id == ref.source_id:
                    return False
        return True

    def combos_for(case):
        roles = [req.role for req in case.requirements]
        candidate_lists = [
            candidates.get((case.case_id, role), []) for role in roles
        ]
        results = []
        for picked in itertools.product(*candidate_lists):
            if len(set(picked)) != len(picked):
                continue
            assignment = dict(zip(roles, picked))
            if not constraints_ok(case, assignment):
                continue
            sources = frozenset(log_by_id[lid].source_id for lid in picked)
            results.append((assignment, sources))
        # Deduplicate by source set, keeping the smallest log-id tuple.
        best_by_sources = {}
        for assignment, sources in results:
            key = tuple(assignment[role] for role in roles)
            if sources not in best_by_sources or key < best_by_sources[sources][0]:
                best_by_sources[sources] = (key, assignment)
        return list(best_by_sources.values())

    per_case_combos = {case.case_id: combos_for(case) for case in cases}
    tiers = sorted({case.tier for case in cases}, reverse=True)

    ordered_cases = sorted(cases, key=lambda c: (-c.tier, c.case_id))
    best = None
    best_key = None

    def record(selection, sources, cost):
        nonlocal best, best_key
        score = tuple(
            sum(
                1
                for case_id, _assign in selection
                if next(c for c in cases if c.case_id == case_id).tier == tier
            )
            for tier in tiers
        )
        plan_key = tuple(
            (
                case_id,
                tuple(
                    (role, assign[role])
                    for role in [r.role for r in requirements[case_id]]
                ),
            )
            for case_id, assign in selection
        )
        key = (score, -cost)
        if best_key is None or key > best_key[0] or (
            key == best_key[0] and plan_key < best_key[1]
        ):
            best_key = (key, plan_key)
            best = (selection, sources, cost)

    def recurse(index, selection, sources, cost):
        if cost > budget:
            return
        if index == len(ordered_cases):
            record(selection, sources, cost)
            return
        case = ordered_cases[index]
        # leave unproven
        recurse(index + 1, selection, sources, cost)
        for _key, assignment in per_case_combos[case.case_id]:
            combo_sources = frozenset(
                log_by_id[assignment[role]].source_id
                for role in (r.role for r in case.requirements)
            )
            recurse(
                index + 1,
                selection + ((case.case_id, dict(assignment)),),
                sources | combo_sources,
                cost + sum(source_size[s] for s in combo_sources - sources),
            )

    recurse(0, (), frozenset(), 0)
    return best


class BruteForceCrossCheckTests(unittest.TestCase):
    """Verify the solver against exhaustive enumeration on small scenarios."""

    def _check(self, logs, cases, links, budget):
        expected = brute_force_plan(logs, cases, links, budget)
        plan = build_plan(make_request(logs, cases, links, budget))
        self.assertTrue(plan.optimal)

        if expected is None:
            self.assertEqual(plan.satisfied_case_ids, ())
            return

        expected_selection, expected_sources, expected_cost = expected
        expected_cases = {case_id for case_id, _ in expected_selection}
        self.assertEqual(set(plan.satisfied_case_ids), expected_cases)
        self.assertEqual(plan.total_size, expected_cost)
        self.assertEqual({s.source_id for s in plan.sources}, set(expected_sources))

        # Every retained source is genuinely used and charged once.
        self.assertLessEqual(plan.total_size, budget)
        used_sources = set()
        for outcome in plan.cases:
            if outcome.satisfied:
                for choice in outcome.evidence:
                    used_sources.add(choice.source_id)
        self.assertEqual({s.source_id for s in plan.sources}, used_sources)

    def test_shared_and_competing_sources(self):
        logs = [
            LogRecord("a", "sA", 10, 40),
            LogRecord("b", "sB", 20, 30),
            LogRecord("c", "sC", 30, 30),
            LogRecord("d", "sD", 40, 20),
            LogRecord("e", "sE", 50, 20),
        ]
        cases = [CaseSpec("c1", 2), CaseSpec("c2", 1), CaseSpec("c3", 1)]
        links = [
            EvidenceLink("c1", "initiation", "a"),
            EvidenceLink("c1", "approval", "b"),
            EvidenceLink("c1", "effect", "c"),
            EvidenceLink("c2", "initiation", "a"),
            EvidenceLink("c2", "approval", "c"),
            EvidenceLink("c2", "effect", "d"),
            EvidenceLink("c3", "initiation", "b"),
            EvidenceLink("c3", "approval", "d"),
            EvidenceLink("c3", "effect", "e"),
        ]
        for budget in range(0, 130, 10):
            with self.subTest(budget=budget):
                self._check(logs, cases, links, budget)

    def test_multi_candidate_roles(self):
        logs = [
            LogRecord("i1", "sI1", 5, 10),
            LogRecord("i2", "sI2", 15, 12),
            LogRecord("a1", "sA1", 20, 8),
            LogRecord("a2", "sA2", 25, 9),
            LogRecord("e1", "sE1", 30, 11),
            LogRecord("e2", "sE2", 35, 7),
        ]
        cases = [CaseSpec("c1", 1)]
        links = []
        for role in ("initiation", "approval", "effect"):
            for log in logs:
                if log.log_id.startswith(role[0]):
                    links.append(EvidenceLink("c1", role, log.log_id))
        for budget in range(0, 60, 3):
            with self.subTest(budget=budget):
                self._check(logs, cases, links, budget)

    def test_copy_based_independence(self):
        logs = [
            LogRecord("a", "sA", 10, 25),
            LogRecord("a_copy", "sA", 10, 25, copied_from="a"),
            LogRecord("b", "sB", 20, 15),
            LogRecord("c", "sC", 30, 15),
            LogRecord("d", "sD", 40, 15),
        ]
        cases = [CaseSpec("c1", 1), CaseSpec("c2", 1)]
        links = [
            EvidenceLink("c1", "initiation", "a"),
            EvidenceLink("c1", "approval", "a_copy"),
            EvidenceLink("c1", "effect", "b"),
            EvidenceLink("c2", "initiation", "a_copy"),
            EvidenceLink("c2", "approval", "c"),
            EvidenceLink("c2", "effect", "d"),
        ]
        for budget in range(0, 80, 5):
            with self.subTest(budget=budget):
                self._check(logs, cases, links, budget)

    def test_tier_priority(self):
        logs = [
            LogRecord("h1", "sH1", 10, 30),
            LogRecord("h2", "sH2", 20, 20),
            LogRecord("h3", "sH3", 30, 20),
            LogRecord("l1", "sL1", 40, 5),
            LogRecord("l2", "sL2", 50, 5),
            LogRecord("l3", "sL3", 60, 5),
            LogRecord("l4", "sL4", 70, 5),
        ]
        cases = [CaseSpec("high", 3), CaseSpec("low1", 1), CaseSpec("low2", 1)]
        links = [
            EvidenceLink("high", "initiation", "h1"),
            EvidenceLink("high", "approval", "h2"),
            EvidenceLink("high", "effect", "h3"),
            EvidenceLink("low1", "initiation", "l1"),
            EvidenceLink("low1", "approval", "l2"),
            EvidenceLink("low1", "effect", "l3"),
            EvidenceLink("low2", "initiation", "l1"),
            EvidenceLink("low2", "approval", "l2"),
            EvidenceLink("low2", "effect", "l4"),
        ]
        for budget in range(0, 90, 5):
            with self.subTest(budget=budget):
                self._check(logs, cases, links, budget)


class InvariantTests(unittest.TestCase):
    """Structural invariants that must hold for any returned plan."""

    def _assert_invariants(self, plan, logs, cases, budget):
        log_by_id = {log.log_id: log for log in logs}
        self.assertLessEqual(plan.total_size, budget)
        self.assertEqual(
            plan.total_size, sum(source.size for source in plan.sources)
        )
        self.assertEqual(len({s.source_id for s in plan.sources}), len(plan.sources))

        for outcome in plan.cases:
            if not outcome.satisfied:
                self.assertEqual(outcome.evidence, ())
                continue
            log_ids = [choice.log_id for choice in outcome.evidence]
            self.assertEqual(len(log_ids), len(set(log_ids)))
            spec = next(c for c in cases if c.case_id == outcome.case_id)
            self.assertEqual(
                {choice.role for choice in outcome.evidence},
                {req.role for req in spec.requirements},
            )
            for choice in outcome.evidence:
                self.assertIn(choice.log_id, log_by_id)
                self.assertEqual(
                    choice.source_id, log_by_id[choice.log_id].source_id
                )
                self.assertIn(choice.log_id, {r.log_id for r in plan.logs})
            for req in spec.requirements:
                chosen = {c.role: log_by_id[c.log_id] for c in outcome.evidence}
                if req.after is not None:
                    if req.strict_after:
                        self.assertGreater(
                            chosen[req.role].timestamp,
                            chosen[req.after].timestamp,
                        )
                    else:
                        self.assertGreaterEqual(
                            chosen[req.role].timestamp,
                            chosen[req.after].timestamp,
                        )
                if req.distinct_source_from is not None:
                    self.assertNotEqual(
                        chosen[req.role].source_id,
                        chosen[req.distinct_source_from].source_id,
                    )

    def test_randomised_small_scenarios(self):
        import random

        rng = random.Random(20261127)
        for trial in range(40):
            n_logs = rng.randint(3, 7)
            logs = []
            for i in range(n_logs):
                source = f"s{rng.randint(0, n_logs - 1)}"
                logs.append(
                    LogRecord(
                        f"l{i}",
                        source,
                        rng.randint(0, 5),
                        rng.choice([0, 5, 10, 15, 20]),
                    )
                )
            cases = [
                CaseSpec(f"c{i}", rng.randint(1, 3)) for i in range(rng.randint(1, 3))
            ]
            links = []
            for case in cases:
                for role in ("initiation", "approval", "effect"):
                    for log in logs:
                        if rng.random() < 0.45:
                            links.append(EvidenceLink(case.case_id, role, log.log_id))
            budget = rng.choice([0, 10, 20, 30, 40, 60, 80])
            with self.subTest(trial=trial):
                plan = build_plan(make_request(logs, cases, links, budget))
                self._assert_invariants(plan, logs, cases, budget)
                expected = brute_force_plan(logs, cases, links, budget)
                if expected is None:
                    self.assertEqual(plan.satisfied_case_ids, ())
                else:
                    exp_selection, exp_sources, exp_cost = expected
                    self.assertEqual(
                        set(plan.satisfied_case_ids),
                        {cid for cid, _ in exp_selection},
                    )
                    self.assertEqual(plan.total_size, exp_cost)
                    self.assertEqual(
                        {s.source_id for s in plan.sources}, set(exp_sources)
                    )


if __name__ == "__main__":
    unittest.main()
