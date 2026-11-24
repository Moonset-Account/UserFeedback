from __future__ import annotations

import json
import re
import unittest

from forensic_allocator import (
    AllocationError,
    Exhibit,
    Reviewer,
    UnknownReviewerError,
    ValidationError,
    allocate,
    dispatch_view,
    reviewer_case_numbers,
    reviewer_task_payload,
    reviewer_tasks,
)


TOKEN_RE = re.compile(r"^ANON-[0-9A-F]{8}(?:-[0-9A-F]{8}){3}$")


def q(*values: str) -> frozenset[str]:
    return frozenset(values)


class BasicAllocationTests(unittest.TestCase):
    def test_assigns_two_qualified_reviewers_from_different_labs(self) -> None:
        result = allocate(
            [Exhibit("E1", q("DNA"), "G1", priority=5)],
            [
                Reviewer("A", "L1", q("DNA"), capacity=2, name="Alice"),
                Reviewer("B", "L2", q("DNA"), capacity=2, name="Bob"),
            ],
            anonymous_secret="stable-secret",
        )

        self.assertEqual(result.coverage_count, 1)
        self.assertEqual(result.maximum_coverage, 1)
        assignment = result.assignments[0]
        self.assertEqual(assignment.exhibit_id, "E1")
        self.assertEqual(set(assignment.reviewer_ids), {"A", "B"})
        self.assertNotEqual(assignment.labs[0], assignment.labs[1])
        self.assertEqual(result.loads, {"A": 1, "B": 1})
        self.assertEqual(assignment.anonymous_case_numbers[0],
                         assignment.anonymous_case_numbers[1])
        for token in assignment.anonymous_case_numbers:
            self.assertRegex(token, TOKEN_RE)

    def test_accepts_plain_dicts_and_chinese_keys(self) -> None:
        result = allocate(
            [
                {
                    "证物ID": "证物-1",
                    "所需资质": ["电子取证"],
                    "同源组": "批次A",
                    "优先级": 1,
                }
            ],
            [
                {
                    "复核人ID": "r1",
                    "姓名": "甲",
                    "所属实验室": "实验室一",
                    "资质": ["电子取证"],
                    "可接收件数": 1,
                    "接触同源组": [],
                },
                {
                    "复核人ID": "r2",
                    "姓名": "乙",
                    "所属实验室": "实验室二",
                    "资质": ["电子取证"],
                    "可接收件数": 1,
                },
            ],
        )
        self.assertEqual(result.coverage_exhibit_ids, ("证物-1",))
        self.assertEqual(result.assignments[0].labs, ("实验室一", "实验室二"))

    def test_same_lab_pair_is_rejected_and_alternative_is_used(self) -> None:
        result = allocate(
            [Exhibit("E1", q("Q"))],
            [
                Reviewer("A", "L1", q("Q")),
                Reviewer("B", "L1", q("Q")),
                Reviewer("C", "L2", q("Q")),
            ],
            anonymous_secret="s",
        )
        self.assertEqual(result.coverage_count, 1)
        self.assertEqual(result.assignments[0].reviewer_ids, ("A", "C"))
        self.assertEqual(result.assignments[0].labs, ("L1", "L2"))

    def test_same_source_group_contact_requires_recusal(self) -> None:
        result = allocate(
            [Exhibit("E1", q("Q"), source_group="G7")],
            [
                Reviewer("A", "L1", q("Q"), exposed_source_groups=q("G7")),
                Reviewer("B", "L2", q("Q")),
                Reviewer("C", "L3", q("Q")),
            ],
        )
        self.assertEqual(result.coverage_count, 1)
        self.assertEqual(result.assignments[0].reviewer_ids, ("B", "C"))
        report = dispatch_view(result)
        self.assertEqual(report["unassigned"], [])

    def test_direct_prior_exhibit_contact_requires_recusal(self) -> None:
        result = allocate(
            [Exhibit("E1", q("Q"))],
            [
                Reviewer("A", "L1", q("Q"), recused_exhibit_ids=q("E1")),
                Reviewer("B", "L2", q("Q")),
                Reviewer("C", "L3", q("Q")),
            ],
        )
        self.assertEqual(result.assignments[0].reviewer_ids, ("B", "C"))

    def test_recusal_maps_can_be_supplied_separately(self) -> None:
        result = allocate(
            [Exhibit("E1", q("Q"), source_group="G7")],
            [
                Reviewer("A", "L1", q("Q")),
                Reviewer("B", "L2", q("Q")),
                Reviewer("C", "L3", q("Q")),
                Reviewer("D", "L4", q("Q")),
            ],
            source_group_recusals={"G7": ["A"]},
            exhibit_recusals={"E1": ["B"]},
        )
        self.assertEqual(result.coverage_count, 1)
        self.assertEqual(result.assignments[0].reviewer_ids, ("C", "D"))

    def test_one_reviewer_is_not_a_complete_assignment(self) -> None:
        result = allocate(
            [Exhibit("E1", q("Q"))],
            [Reviewer("A", "L1", q("Q"), capacity=3)],
        )
        self.assertEqual(result.coverage_count, 0)
        self.assertEqual(len(result.unassigned), 1)
        report = result.unassigned[0]
        self.assertEqual(report.primary_reason, "no_eligible_pair")
        self.assertEqual(report.cross_lab_pair_count, 0)


class DiagnosticTests(unittest.TestCase):
    def test_reports_individual_qualification_gap(self) -> None:
        result = allocate(
            [Exhibit("E1", q("DNA"))],
            [
                Reviewer("A", "L1", q("DNA")),
                Reviewer("B", "L2", q("FINGERPRINT")),
            ],
        )
        report = result.unassigned[0]
        self.assertEqual(report.primary_reason, "qualification_gap")
        self.assertIn("insufficient_qualified_reviewers", report.reason_codes)
        self.assertEqual(report.eligible_reviewer_ids, ("A",))
        gaps = {gap.reviewer_id: gap.missing_qualifications
                for gap in report.reviewer_qualification_gaps}
        self.assertEqual(gaps["B"], ("DNA",))

    def test_reports_qualification_held_by_nobody(self) -> None:
        result = allocate(
            [Exhibit("E1", q("DNA", "TOX"))],
            [
                Reviewer("A", "L1", q("DNA")),
                Reviewer("B", "L2", q("DNA")),
            ],
        )
        report = result.unassigned[0]
        self.assertEqual(report.primary_reason, "qualification_gap")
        self.assertEqual(report.missing_qualifications, ("TOX",))
        self.assertIn("unheld_qualification", report.reason_codes)

    def test_reports_laboratory_diversity_constraint(self) -> None:
        result = allocate(
            [Exhibit("E1", q("Q"))],
            [
                Reviewer("A", "L1", q("Q")),
                Reviewer("B", "L1", q("Q")),
            ],
        )
        report = result.unassigned[0]
        self.assertEqual(report.primary_reason, "lab_constraint")
        self.assertIn("same_lab_constraint", report.reason_codes)
        self.assertEqual(report.cross_lab_pair_count, 0)

    def test_zero_capacity_is_reported_as_quota_not_qualification_gap(self) -> None:
        result = allocate(
            [Exhibit("E1", q("Q"))],
            [
                Reviewer("A", "L1", q("Q"), capacity=0),
                Reviewer("B", "L2", q("Q"), capacity=0),
            ],
        )
        report = result.unassigned[0]
        self.assertEqual(result.coverage_count, 0)
        self.assertEqual(report.primary_reason, "capacity_constraint")
        self.assertEqual(report.cross_lab_pair_count, 1)
        self.assertEqual(
            report.candidate_pairs[0].saturated_blocker_ids, ("A", "B")
        )
        self.assertEqual(report.minimum_additional_slots, 2)

    def test_capacity_competition_identifies_saturated_candidates(self) -> None:
        reviewers = [
            Reviewer("A", "L1", q("Q", "X"), capacity=1),
            Reviewer("B", "L2", q("Q", "X"), capacity=1),
            Reviewer("C", "L3", q("Q"), capacity=1),
            Reviewer("D", "L4", q("Q"), capacity=1),
        ]
        exhibits = [
            Exhibit("E1", q("Q"), priority=100),
            Exhibit("E2", q("Q", "X"), priority=90),
            Exhibit("E3", q("Q"), priority=80),
        ]
        result = allocate(exhibits, reviewers)

        self.assertEqual(result.maximum_coverage, 2)
        self.assertEqual(result.coverage_exhibit_ids, ("E1", "E2"))
        by_exhibit = {a.exhibit_id: a.reviewer_ids for a in result.assignments}
        # Optimizer must avoid the lexicographically first (A,B) pair for E1,
        # because consuming both X-qualified reviewers would lose E2.
        self.assertEqual(by_exhibit["E1"], ("C", "D"))
        self.assertEqual(by_exhibit["E2"], ("A", "B"))

        report = next(item for item in result.unassigned if item.exhibit_id == "E3")
        self.assertEqual(report.primary_reason, "capacity_constraint")
        self.assertGreater(report.cross_lab_pair_count, 0)
        for pair in report.candidate_pairs:
            self.assertTrue(pair.saturated_blocker_ids)
        self.assertEqual(result.loads, {"A": 1, "B": 1, "C": 1, "D": 1})


class PriorityTieBreakTests(unittest.TestCase):
    def test_lexicographic_priority_selects_highest_ranked_cover(self) -> None:
        reviewers = [
            Reviewer("A", "L1", q("Q"), capacity=1),
            Reviewer("B", "L2", q("Q"), capacity=1),
            Reviewer("C", "L3", q("Q"), capacity=1),
            Reviewer("D", "L4", q("Q"), capacity=1),
        ]
        exhibits = [
            Exhibit("LOW", q("Q"), priority=1),
            Exhibit("HIGH", q("Q"), priority=10),
            Exhibit("MID", q("Q"), priority=5),
        ]
        result = allocate(exhibits, reviewers)
        self.assertEqual(result.maximum_coverage, 2)
        self.assertEqual(set(result.coverage_exhibit_ids), {"HIGH", "MID"})

    def test_lower_priority_number_can_mean_highest_rank(self) -> None:
        reviewers = [
            Reviewer("A", "L1", q("Q"), capacity=1),
            Reviewer("B", "L2", q("Q"), capacity=1),
            Reviewer("C", "L3", q("Q"), capacity=1),
            Reviewer("D", "L4", q("Q"), capacity=1),
        ]
        exhibits = [
            Exhibit("RANK3", q("Q"), priority=3),
            Exhibit("RANK1", q("Q"), priority=1),
            Exhibit("RANK2", q("Q"), priority=2),
        ]
        result = allocate(
            exhibits, reviewers, priority_direction="smaller"
        )
        self.assertEqual(set(result.coverage_exhibit_ids), {"RANK1", "RANK2"})


class PrivacyViewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.exhibits = [Exhibit("SECRET-E1", q("Q"), "SECRET-G")]
        self.reviewers = [
            Reviewer("r-Alice", "L1", q("Q"), name="Alice Secret"),
            Reviewer("r-Bob", "L2", q("Q"), name="Bob Secret"),
        ]

    def test_reviewer_view_contains_only_own_anonymous_number(self) -> None:
        result = allocate(
            self.exhibits, self.reviewers, anonymous_secret="privacy-test"
        )
        tasks = reviewer_tasks(result, "r-Alice")
        self.assertEqual(len(tasks), 1)
        self.assertRegex(tasks[0].anonymous_case_number, TOKEN_RE)
        payload_data = reviewer_task_payload(result, "r-Alice")
        self.assertEqual(set(payload_data[0].keys()), {"anonymous_case_number"})

        payload = json.dumps(payload_data, ensure_ascii=False)
        for secret in ("SECRET-E1", "SECRET-G", "r-Alice", "r-Bob",
                       "Alice Secret", "Bob Secret", "L1", "L2"):
            self.assertNotIn(secret, payload)
        self.assertIn("anonymous_case_number", payload)

    def test_review_scope_gives_unlinkable_seat_specific_numbers(self) -> None:
        result = allocate(
            self.exhibits,
            self.reviewers,
            anonymous_scope="review",
            anonymous_secret="privacy-test",
        )
        alice = reviewer_tasks(result, "r-Alice")
        bob = reviewer_tasks(result, "r-Bob")
        self.assertNotEqual(
            alice[0].anonymous_case_number, bob[0].anonymous_case_number
        )
        alice_payload = json.dumps(
            reviewer_task_payload(result, "r-Alice")
        )
        self.assertNotIn(bob[0].anonymous_case_number, alice_payload)

    def test_reviewer_view_filters_only_that_reviewers_tasks(self) -> None:
        exhibits = [
            Exhibit("E1", frozenset({"Q"})),
            Exhibit("E2", frozenset({"Q"})),
        ]
        reviewers = [
            Reviewer("A", "L1", frozenset({"Q"}), capacity=2),
            Reviewer("B", "L2", frozenset({"Q"}), capacity=2),
            Reviewer("C", "L3", frozenset({"Q"}), capacity=2),
        ]
        result = allocate(exhibits, reviewers, anonymous_secret="filter")

        for reviewer in reviewers:
            with self.subTest(reviewer=reviewer.reviewer_id):
                expected: set[str] = set()
                for assignment in result.assignments:
                    if reviewer.reviewer_id in assignment.reviewer_ids:
                        index = assignment.reviewer_ids.index(
                            reviewer.reviewer_id
                        )
                        expected.add(
                            assignment.anonymous_case_numbers[index]
                        )
                actual = set(reviewer_case_numbers(result, reviewer.reviewer_id))
                self.assertEqual(actual, expected)
                for row in reviewer_task_payload(
                    result, reviewer.reviewer_id
                ):
                    self.assertEqual(
                        set(row.keys()), {"anonymous_case_number"}
                    )

    def test_unknown_reviewer_view_is_rejected(self) -> None:
        result = allocate(self.exhibits, self.reviewers)
        with self.assertRaises(UnknownReviewerError):
            reviewer_tasks(result, "nobody")

    def test_secret_based_anonymous_numbers_are_stable(self) -> None:
        first = allocate(
            self.exhibits, self.reviewers, anonymous_secret="same-key"
        )
        second = allocate(
            self.exhibits, self.reviewers, anonymous_secret="same-key"
        )
        self.assertEqual(
            first.assignments[0].anonymous_case_numbers,
            second.assignments[0].anonymous_case_numbers,
        )

    def test_random_anonymous_numbers_do_not_repeat_for_seats(self) -> None:
        result = allocate(
            self.exhibits,
            self.reviewers,
            anonymous_scope="review",
        )
        tokens = result.assignments[0].anonymous_case_numbers
        self.assertEqual(len(tokens), 2)
        self.assertEqual(len(set(tokens)), 2)

    def test_dispatch_view_contains_named_allocation(self) -> None:
        result = allocate(
            self.exhibits, self.reviewers, anonymous_secret="dispatch-test"
        )
        view = dispatch_view(result)
        payload = json.dumps(view, ensure_ascii=False)
        self.assertIn("SECRET-E1", payload)
        self.assertIn("Alice Secret", payload)
        self.assertIn("Bob Secret", payload)
        self.assertIn("r-Alice", payload)
        self.assertIn("anonymous_directory", view)


class ValidationTests(unittest.TestCase):
    def test_duplicate_ids_are_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            allocate(
                [Exhibit("E1"), Exhibit("E1")],
                [Reviewer("A", "L1"), Reviewer("B", "L2")],
            )
        with self.assertRaises(ValidationError):
            allocate(
                [Exhibit("E1")],
                [Reviewer("A", "L1"), Reviewer("A", "L2")],
            )

    def test_empty_collections_are_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            allocate([], [Reviewer("A", "L1")])
        with self.assertRaises(ValidationError):
            allocate([Exhibit("E1")], [])

    def test_negative_capacity_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            allocate(
                [Exhibit("E1")],
                [Reviewer("A", "L1", capacity=-1), Reviewer("B", "L2")],
            )

    def test_invalid_options_are_rejected(self) -> None:
        ex = [Exhibit("E1")]
        rv = [Reviewer("A", "L1"), Reviewer("B", "L2")]
        with self.assertRaises(AllocationError):
            allocate(ex, rv, tie_break="bogus")
        with self.assertRaises(ValidationError):
            allocate(ex, rv, anonymous_secret="")
        with self.assertRaises(ValidationError):
            allocate(
                ex,
                rv,
                source_group_recusals={"G": ["unknown-reviewer"]},
            )


if __name__ == "__main__":
    unittest.main()
