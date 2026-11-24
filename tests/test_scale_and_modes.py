from __future__ import annotations

import unittest

from forensic_allocator import Exhibit, Reviewer, allocate


class ScaleAndModeTests(unittest.TestCase):
    def test_twelve_exhibit_synthetic_batch_is_completely_covered(self) -> None:
        exhibits = [
            Exhibit(f"E{i:02d}", frozenset({"TRACE"}), priority=i)
            for i in range(12)
        ]
        reviewers = [
            Reviewer(
                f"R{i}",
                f"L{i % 4}",
                frozenset({"TRACE"}),
                capacity=3,
            )
            for i in range(8)
        ]

        result = allocate(exhibits, reviewers, anonymous_secret="scale-test")

        self.assertEqual(result.maximum_coverage, 12)
        self.assertEqual(result.coverage_count, 12)
        self.assertEqual(result.unassigned, ())
        for assignment in result.assignments:
            self.assertNotEqual(assignment.labs[0], assignment.labs[1])
        self.assertTrue(all(load == 3 for load in result.loads.values()))

    def test_twelve_exhibit_capacity_limited_case_remains_exact(self) -> None:
        exhibits = [Exhibit(f"E{i:02d}", frozenset({"Q"})) for i in range(12)]
        reviewers = [
            Reviewer(f"R{i}", f"L{i}", frozenset({"Q"}), capacity=2)
            for i in range(8)
        ]

        result = allocate(exhibits, reviewers, anonymous_secret="capacity-test")

        # 8 reviewers * 2 slots = 16 reviewer-slots, and each cover uses 2.
        self.assertEqual(result.maximum_coverage, 8)
        self.assertEqual(result.coverage_count, 8)
        self.assertEqual(len(result.unassigned), 4)
        self.assertTrue(
            all(report.primary_reason == "capacity_constraint"
                for report in result.unassigned)
        )

    def test_collective_qualification_mode_combines_pair_skills(self) -> None:
        exhibit = Exhibit("E1", frozenset({"PHOTO", "DNA"}))
        reviewers = [
            Reviewer("A", "L1", frozenset({"PHOTO"})),
            Reviewer("B", "L2", frozenset({"DNA"})),
        ]

        each_mode = allocate([exhibit], reviewers)
        self.assertEqual(each_mode.coverage_count, 0)
        self.assertEqual(
            each_mode.unassigned[0].primary_reason, "qualification_gap"
        )

        collective = allocate(
            [exhibit], reviewers, qualification_mode="collective"
        )
        self.assertEqual(collective.coverage_count, 1)
        self.assertEqual(
            collective.assignments[0].reviewer_ids, ("A", "B")
        )

    def test_priority_sum_mode_is_accepted_and_selects_high_sum(self) -> None:
        reviewers = [
            Reviewer("A", "L1", frozenset({"Q"}), capacity=1),
            Reviewer("B", "L2", frozenset({"Q"}), capacity=1),
            Reviewer("C", "L3", frozenset({"Q"}), capacity=1),
            Reviewer("D", "L4", frozenset({"Q"}), capacity=1),
        ]
        exhibits = [
            Exhibit("E1", frozenset({"Q"}), priority=10),
            Exhibit("E2", frozenset({"Q"}), priority=9),
            Exhibit("E3", frozenset({"Q"}), priority=1),
        ]
        result = allocate(exhibits, reviewers, tie_break="priority_sum")
        self.assertEqual(
            set(result.coverage_exhibit_ids), {"E1", "E2"}
        )
        self.assertEqual(result.tie_break, "priority_sum")


if __name__ == "__main__":
    unittest.main()
