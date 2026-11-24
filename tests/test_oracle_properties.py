from __future__ import annotations

import random
import unittest

from forensic_allocator import Exhibit, Reviewer, allocate


def brute_force_best(
    exhibits: list[Exhibit],
    reviewers: list[Reviewer],
    *,
    direction: str,
    tie_break: str,
) -> frozenset[str]:
    """Independent tiny oracle: skip an exhibit or choose one legal pair."""

    def rank_key(exhibit: Exhibit, original_index: int):
        rank_value = -exhibit.priority if direction == "larger" else exhibit.priority
        return (rank_value, original_index)

    ranked = [
        exhibit
        for _, exhibit in sorted(
            enumerate(exhibits), key=lambda item: rank_key(item[1], item[0])
        )
    ]

    def weight(exhibit: Exhibit) -> float:
        return exhibit.priority if direction == "larger" else -exhibit.priority

    loads = [0] * len(reviewers)
    selected: set[str] = set()
    best_key: tuple[object, ...] | None = None
    best_set: frozenset[str] = frozenset()

    def legal_pair(exhibit: Exhibit, i: int, j: int) -> bool:
        left = reviewers[i]
        right = reviewers[j]
        if not left.active or not right.active:
            return False
        if exhibit.source_group is not None and (
            exhibit.source_group in left.exposed_source_groups
            or exhibit.source_group in right.exposed_source_groups
        ):
            return False
        if exhibit.exhibit_id in left.recused_exhibit_ids:
            return False
        if exhibit.exhibit_id in right.recused_exhibit_ids:
            return False
        if left.lab == right.lab:
            return False
        return (
            exhibit.required_qualifications <= left.qualifications
            and exhibit.required_qualifications <= right.qualifications
        )

    def visit(index: int) -> None:
        nonlocal best_key, best_set

        if index == len(ranked):
            inclusion = tuple(exhibit.exhibit_id in selected for exhibit in ranked)
            count = len(selected)
            if tie_break == "lexicographic":
                key: tuple[object, ...] = (count, inclusion)
            else:
                score = sum(weight(e) for e in ranked if e.exhibit_id in selected)
                key = (count, score, inclusion)
            if best_key is None or key > best_key:
                best_key = key
                best_set = frozenset(selected)
            return

        exhibit = ranked[index]

        # Option 1: leave this exhibit uncovered.
        visit(index + 1)

        # Option 2: assign one legal pair with remaining quota.
        for i in range(len(reviewers)):
            for j in range(i + 1, len(reviewers)):
                if not legal_pair(exhibit, i, j):
                    continue
                if loads[i] + 1 > reviewers[i].capacity:
                    continue
                if loads[j] + 1 > reviewers[j].capacity:
                    continue
                loads[i] += 1
                loads[j] += 1
                selected.add(exhibit.exhibit_id)
                visit(index + 1)
                selected.remove(exhibit.exhibit_id)
                loads[i] -= 1
                loads[j] -= 1

    visit(0)
    return best_set


class RandomizedOracleTests(unittest.TestCase):
    def test_small_instances_match_exhaustive_oracle(self) -> None:
        rng = random.Random(20261125)
        qualification_pool = ("DNA", "DIGITAL", "CHEMISTRY")
        checked = 0

        for seed in range(60):
            rng.seed(1000 + seed)
            exhibit_count = rng.randint(2, 5)
            reviewer_count = rng.randint(2, 6)
            direction = rng.choice(("larger", "smaller"))

            exhibits: list[Exhibit] = []
            for index in range(exhibit_count):
                required = frozenset(
                    q
                    for q in qualification_pool
                    if rng.random() < rng.choice((0.25, 0.45, 0.65))
                )
                source_group = rng.choice(("G0", "G1", None))
                exhibits.append(
                    Exhibit(
                        f"E{index}",
                        required,
                        source_group,
                        priority=rng.randint(1, 4),
                    )
                )

            reviewers: list[Reviewer] = []
            for index in range(reviewer_count):
                quals = frozenset(
                    q for q in qualification_pool if rng.random() < 0.65
                )
                exposed = frozenset(
                    group for group in ("G0", "G1") if rng.random() < 0.22
                )
                reviewers.append(
                    Reviewer(
                        f"R{index}",
                        rng.choice(("L1", "L2", "L3")),
                        quals,
                        capacity=rng.randint(0, 2),
                        exposed_source_groups=exposed,
                    )
                )

            for tie_break in ("lexicographic", "priority_sum"):
                with self.subTest(seed=seed, tie_break=tie_break):
                    expected = brute_force_best(
                        exhibits,
                        reviewers,
                        direction=direction,
                        tie_break=tie_break,
                    )
                    result = allocate(
                        exhibits,
                        reviewers,
                        tie_break=tie_break,
                        priority_direction=direction,
                        anonymous_secret=f"seed-{seed}",
                    )
                    actual = frozenset(result.coverage_exhibit_ids)
                    self.assertEqual(result.maximum_coverage, len(expected))
                    self.assertEqual(actual, expected)

                    reviewer_by_id = {r.reviewer_id: r for r in reviewers}
                    for assignment in result.assignments:
                        ex = next(
                            e
                            for e in exhibits
                            if e.exhibit_id == assignment.exhibit_id
                        )
                        left = reviewer_by_id[assignment.reviewer_ids[0]]
                        right = reviewer_by_id[assignment.reviewer_ids[1]]
                        self.assertNotEqual(left.reviewer_id, right.reviewer_id)
                        self.assertNotEqual(left.lab, right.lab)
                        self.assertTrue(
                            ex.required_qualifications <= left.qualifications
                        )
                        self.assertTrue(
                            ex.required_qualifications <= right.qualifications
                        )
                        if ex.source_group is not None:
                            self.assertNotIn(
                                ex.source_group, left.exposed_source_groups
                            )
                            self.assertNotIn(
                                ex.source_group, right.exposed_source_groups
                            )

                    for reviewer_id, load in result.loads.items():
                        self.assertGreaterEqual(load, 0)
                        self.assertLessEqual(
                            load, reviewer_by_id[reviewer_id].capacity
                        )

                    for report in result.unassigned:
                        self.assertNotIn(report.exhibit_id, actual)
                        if report.primary_reason == "capacity_constraint":
                            self.assertGreater(report.cross_lab_pair_count, 0)
                            for pair in report.candidate_pairs:
                                self.assertTrue(pair.saturated_blocker_ids)
                        else:
                            self.assertEqual(report.cross_lab_pair_count, 0)
                    checked += 1

        self.assertEqual(checked, 120)


if __name__ == "__main__":
    unittest.main()
