from __future__ import annotations

import json
import unittest
from itertools import product
from random import Random

from packet_planner import (
    InfeasiblePlanError,
    PacketPlanner,
    PaddingBudgetExceeded,
    PaddingLimit,
    PlanningError,
    RecordDoesNotFitError,
    plan_packets,
    try_plan_packets,
)


def feasible_by_enumeration(records, lengths):
    """Independent oracle: try every ordered cut for every segment."""

    n = len(records)
    segment_count = len(lengths)

    def search(segment_index, record_index):
        if segment_index == segment_count:
            return record_index == n

        # end == record_index represents a full-padding segment.
        segment_sum = 0
        for end in range(record_index, n + 1):
            if end > record_index:
                segment_sum += records[end - 1]
            if segment_sum > lengths[segment_index]:
                break
            if search(segment_index + 1, end):
                return True
        return False

    return search(0, 0)


def brute_force_best(materials, sizes, max_segments):
    """Exhaustively enumerate every ordered profile, including length zero."""

    best = None
    for count in range(0, max_segments + 1):
        for lengths in product(sizes, repeat=count):
            if all(feasible_by_enumeration(records, lengths) for records in materials):
                key = (sum(lengths), len(lengths), lengths)
                if best is None or key < best[0]:
                    best = (key, lengths)
    return None if best is None else best[1]


def assert_plan_invariants(testcase, plan, materials, sizes, max_segments):
    testcase.assertLessEqual(plan.segment_count, max_segments)
    testcase.assertEqual(len(plan.materials), len(materials))
    testcase.assertEqual(plan.common_lengths, tuple(segment.length for segment in plan.materials[0].segments))

    expected_total_records = 0
    for material, original in zip(plan.materials, materials):
        testcase.assertEqual(material.record_lengths, tuple(original))
        testcase.assertEqual(material.segment_count, plan.segment_count)
        testcase.assertEqual(
            tuple(segment.length for segment in material.segments),
            plan.common_lengths,
        )

        reconstructed = []
        for segment in material.segments:
            testcase.assertIn(segment.length, sizes)
            testcase.assertEqual(
                segment.record_lengths,
                tuple(original[i] for i in segment.record_indices),
            )
            testcase.assertGreaterEqual(segment.padding, 0)
            testcase.assertEqual(
                sum(segment.record_lengths) + segment.padding,
                segment.length,
            )
            reconstructed.extend(segment.record_indices)

        testcase.assertEqual(reconstructed, list(range(len(original))))
        testcase.assertEqual(material.padding, sum(plan.common_lengths) - sum(original))
        expected_total_records += sum(original)

    testcase.assertEqual(plan.total_record_bytes, expected_total_records)
    testcase.assertEqual(
        plan.total_profile_bytes,
        len(materials) * sum(plan.common_lengths),
    )
    testcase.assertEqual(
        plan.total_padding,
        plan.total_profile_bytes - plan.total_record_bytes,
    )
    testcase.assertEqual(
        plan.total_padding,
        sum(material.padding for material in plan.materials),
    )

    # The dictionary form must be JSON-serializable.
    json.dumps(plan.to_dict())


class PlannedExamplesTests(unittest.TestCase):
    def test_basic_ordered_packing_and_lex_tie(self):
        materials = [[120, 300, 180]]
        sizes = [200, 500, 1000]
        plan = plan_packets(materials, sizes, 3)

        self.assertEqual(plan.common_lengths, (200, 500))
        self.assertEqual([s.record_lengths for s in plan.materials[0].segments], [(120,), (300, 180)])
        self.assertEqual([s.padding for s in plan.materials[0].segments], [80, 20])
        self.assertEqual(plan.total_padding, 100)
        assert_plan_invariants(self, plan, materials, sizes, 3)

    def test_common_zero_padding_profile(self):
        materials = [[100, 200], [150, 150]]
        sizes = [300, 500]
        plan = plan_packets(materials, sizes, 1)

        self.assertEqual(plan.common_lengths, (300,))
        self.assertEqual(plan.total_padding, 0)
        self.assertEqual(plan.padding_by_material, (0, 0))
        assert_plan_invariants(self, plan, materials, sizes, 1)

    def test_total_capacity_is_not_enough_without_record_fit_check(self):
        # (500, 500) has exactly the same total capacity as record bytes, but
        # the 600-byte record is not intact in either 500-byte segment.
        materials = [[600, 400]]
        sizes = [500, 600]
        plan = plan_packets(materials, sizes, 2)

        self.assertEqual(plan.common_lengths, (600, 500))
        segment = plan.materials[0].segments
        self.assertEqual([s.record_lengths for s in segment], [(600,), (400,)])
        self.assertEqual([s.padding for s in segment], [0, 100])
        assert_plan_invariants(self, plan, materials, sizes, 2)

    def test_trailing_full_padding_segment_is_reported(self):
        materials = [[4], [3, 3]]
        sizes = [4]
        plan = plan_packets(materials, sizes, 2)

        self.assertEqual(plan.common_lengths, (4, 4))
        first, second = plan.materials
        self.assertEqual([s.record_indices for s in first.segments], [(0,), ()])
        self.assertTrue(first.segments[1].is_full_padding)
        self.assertEqual(first.padding, 4)
        self.assertEqual([s.record_lengths for s in second.segments], [(3,), (3,)])
        self.assertEqual(second.padding, 2)
        self.assertEqual(plan.total_padding, 6)
        assert_plan_invariants(self, plan, materials, sizes, 2)

    def test_interior_full_padding_and_segment_order(self):
        # The first material cannot use the initial 2-byte segment; it is a
        # full-padding segment for that material but carries [2] in the second.
        materials = [[5], [2, 4]]
        sizes = [2, 5]
        plan = plan_packets(materials, sizes, 2)

        self.assertEqual(plan.common_lengths, (2, 5))
        first, second = plan.materials
        self.assertEqual([s.record_indices for s in first.segments], [(), (0,)])
        self.assertTrue(first.segments[0].is_full_padding)
        self.assertEqual(first.segments[0].padding, 2)
        self.assertEqual([s.record_indices for s in second.segments], [(0,), (1,)])
        self.assertEqual(plan.total_padding, 3)
        assert_plan_invariants(self, plan, materials, sizes, 2)

    def test_deterministic_count_and_lex_tie_breaks(self):
        # Same total profile bytes (2): one segment is preferred to two.
        plan = plan_packets([[1, 1]], [1, 2], 2)
        self.assertEqual(plan.common_lengths, (2,))

        # Same cost and count; both permutations are feasible, so lex wins.
        plan = plan_packets([[2, 1, 2]], [2, 3], 2)
        self.assertEqual(plan.common_lengths, (2, 3))
        assert_plan_invariants(self, plan, [[2, 1, 2]], [2, 3], 2)

    def test_flat_record_list_is_one_material(self):
        plan = plan_packets([600, 400], [600, 500], 2)
        self.assertEqual(plan.common_lengths, (600, 500))
        self.assertEqual(len(plan.materials), 1)
        self.assertEqual(plan.materials[0].material_id, 0)

    def test_mapping_material_ids_are_preserved(self):
        materials = {"alice": [4], "bob": [3, 3]}
        plan = plan_packets(materials, [4], 2)
        self.assertEqual([m.material_id for m in plan.materials], ["alice", "bob"])
        self.assertEqual(plan.material("alice").padding, 4)

    def test_zero_length_records_are_preserved(self):
        materials = [[0, 2, 0]]
        plan = plan_packets(materials, [2], 1)
        self.assertEqual(plan.common_lengths, (2,))
        self.assertEqual(plan.materials[0].segments[0].record_indices, (0, 1, 2))
        self.assertEqual(plan.total_padding, 0)

    def test_empty_material_profile_options(self):
        plan = plan_packets([[]], [64, 500], 2)
        self.assertEqual(plan.common_lengths, ())
        self.assertEqual(plan.segment_count, 0)
        self.assertEqual(plan.total_padding, 0)

        plan = plan_packets([[]], [64, 500], 2, allow_empty_profile=False)
        self.assertEqual(plan.common_lengths, (64,))
        self.assertTrue(plan.materials[0].segments[0].is_full_padding)
        self.assertEqual(plan.total_padding, 64)

    def test_padding_limits(self):
        materials = [[4], [3, 3]]
        sizes = [4]
        plan = plan_packets(materials, sizes, 2, padding_limit=6)
        self.assertEqual(plan.total_padding, 6)

        with self.assertRaises(PaddingBudgetExceeded) as ctx:
            plan_packets(materials, sizes, 2, padding_limit=5)
        self.assertEqual(ctx.exception.plan.total_padding, 6)
        self.assertIsNone(try_plan_packets(materials, sizes, 2, padding_limit=5))

        plan_packets(
            materials,
            sizes,
            2,
            padding_limit=PaddingLimit(max_per_material=4),
        )
        self.assertIsNone(
            try_plan_packets(
                materials,
                sizes,
                2,
                max_padding_per_material=3,
            )
        )

        # Total record bytes = 10, padding = 6 => overhead ratio 0.6.
        plan_packets(materials, sizes, 2, padding_limit=0.6)
        self.assertIsNone(try_plan_packets(materials, sizes, 2, padding_limit=0.599))

        # Final profile bytes = 16; padding share = 6 / 16 = 0.375.
        plan_packets(
            materials,
            sizes,
            2,
            padding_limit=PaddingLimit.padding_share(0.375),
        )
        self.assertIsNone(
            try_plan_packets(
                materials,
                sizes,
                2,
                padding_limit={"max_padding_fraction": 0.3},
            )
        )

        # Merging keyword limits with a mapping/PaddingLimit must preserve all
        # constraint fields.
        self.assertIsNone(
            try_plan_packets(
                materials,
                sizes,
                2,
                padding_limit={"max_padding_fraction": 0.3},
                max_padding_per_material=100,
            )
        )

    def test_zero_byte_payload_ratio_limit(self):
        plan = plan_packets([[]], [1], 1, allow_empty_profile=False)
        with self.assertRaises(PaddingBudgetExceeded):
            plan_packets(
                [[]],
                [1],
                1,
                allow_empty_profile=False,
                padding_limit=PaddingLimit(max_overhead_ratio=0),
            )
        self.assertEqual(plan.total_padding, 1)

    def test_record_larger_than_every_tier(self):
        materials = [[900, 100]]
        with self.assertRaises(RecordDoesNotFitError) as ctx:
            plan_packets(materials, [200, 500], 3)
        self.assertEqual(ctx.exception.record_length, 900)
        self.assertIsNone(try_plan_packets(materials, [200, 500], 3))

    def test_max_segments_boundary(self):
        # Each 5-byte segment can hold only one 3-byte record.
        materials = [[3, 3, 3]]
        self.assertEqual(plan_packets(materials, [5], 3).common_lengths, (5, 5, 5))

        with self.assertRaises(InfeasiblePlanError):
            plan_packets(materials, [5], 2)
        self.assertIsNone(try_plan_packets(materials, [5], 2))

    def test_invalid_inputs(self):
        bad_calls = [
            lambda: plan_packets([], [8], 1),
            lambda: plan_packets([[1, -2]], [8], 1),
            lambda: plan_packets([[1]], [8, 0], 1),
            lambda: plan_packets([[1]], [8], 0),
            lambda: plan_packets([[1]], [8], True),
            lambda: plan_packets([["x"]], [8], 1),
            lambda: plan_packets([[1]], "8", 1),
            lambda: plan_packets([[1]], [8], 1, padding_limit=-1),
            lambda: plan_packets([[1]], [8], 1, padding_limit=float("nan")),
            lambda: plan_packets(
                [[1]],
                [8],
                1,
                padding_limit=PaddingLimit(max_padding_fraction=1.1),
            ),
        ]
        for call in bad_calls:
            with self.subTest(call=call):
                with self.assertRaises(PlanningError):
                    call()

    def test_planner_wrapper(self):
        planner = PacketPlanner([2, 5], 2, padding_limit=10)
        plan = planner.plan([[5], [2, 4]])
        self.assertIsNotNone(plan)
        self.assertEqual(plan.common_lengths, (2, 5))
        self.assertIsNone(
            PacketPlanner([4], 2, padding_limit=1).plan([[4], [3, 3]], raise_on_infeasible=False)
        )


class ExhaustivePropertyTests(unittest.TestCase):
    def test_dp_matches_independent_bruteforce_on_seeded_cases(self):
        rng = Random(15996)
        sizes_pool = [1, 2, 3, 5]

        for case_number in range(160):
            material_count = rng.randint(1, 3)
            materials = []
            for _ in range(material_count):
                record_count = rng.randint(0, 4)
                # Include zero occasionally to exercise zero-length boundaries.
                materials.append(
                    [rng.choice([0, 1, 1, 2, 3, 4, 6]) for _ in range(record_count)]
                )

            size_count = rng.randint(1, 3)
            sizes = tuple(sorted(set(rng.sample(sizes_pool, size_count))))
            max_segments = rng.randint(1, 4)

            with self.subTest(
                case=case_number,
                materials=materials,
                sizes=sizes,
                max_segments=max_segments,
            ):
                expected = brute_force_best(materials, sizes, max_segments)
                actual = try_plan_packets(materials, sizes, max_segments)

                if expected is None:
                    self.assertIsNone(actual)
                else:
                    self.assertIsNotNone(actual)
                    self.assertEqual(actual.common_lengths, expected)
                    assert_plan_invariants(
                        self, actual, materials, list(sizes), max_segments
                    )


if __name__ == "__main__":
    unittest.main()
