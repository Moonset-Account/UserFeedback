"""无解证书、边界条件与输入校验。"""

import unittest

import _bootstrap  # noqa: F401

from datemapper import (
    ContradictionCertificate,
    DateMapper,
    GapConstraint,
    InfeasibleError,
)


def _certificate_is_independently_valid(certificate: ContradictionCertificate) -> bool:
    """不依赖被测 verify()，按定义独立复核证书。"""

    balance = {}
    total = 0
    for item in certificate.inequalities:
        if item.left is not None:
            balance[item.left] = balance.get(item.left, 0) + 1
        if item.right is not None:
            balance[item.right] = balance.get(item.right, 0) - 1
        total += item.right_hand_side
    return all(value == 0 for value in balance.values()) and total < 0


class InfeasibilityTests(unittest.TestCase):
    def test_same_person_self_contradiction_returns_certificate(self):
        mapper = DateMapper(
            event_dates={1: {"a": 100, "b": 110}},
            offset_bounds={1: (-3, 3)},
            proposed_offsets={1: 0},
            gap_constraints=[GapConstraint((1, "a"), (1, "b"), min_gap_days=20)],
        )
        with self.assertRaises(InfeasibleError) as caught:
            mapper.finalize()
        certificate = caught.exception.certificate
        self.assertTrue(certificate.verify())
        self.assertTrue(_certificate_is_independently_valid(certificate))
        self.assertLess(certificate.contradiction_sum, 0)
        # 单人矛盾坍缩为 x1 - x1 <= 负数 的自环。
        self.assertEqual(len(certificate.inequalities), 1)
        only = certificate.inequalities[0]
        self.assertEqual(only.left, 1)
        self.assertEqual(only.right, 1)
        self.assertEqual(only.right_hand_side, -10)

    def test_cross_person_contradiction_certificate_cancels_variables(self):
        # 要求发布间隔 >= 1000，任何偏移组合都做不到。
        mapper = DateMapper(
            event_dates={1: {"a": 0}, 2: {"b": 10}},
            offset_bounds={1: (-5, 5), 2: (-5, 5)},
            proposed_offsets={1: 0, 2: 0},
            gap_constraints=[GapConstraint((1, "a"), (2, "b"), min_gap_days=1000)],
        )
        with self.assertRaises(InfeasibleError) as caught:
            mapper.finalize()
        certificate = caught.exception.certificate
        self.assertTrue(certificate.verify())
        self.assertTrue(_certificate_is_independently_valid(certificate))
        # 证书文本可读，且包含坍缩结论。
        text = certificate.to_text()
        self.assertIn("矛盾", text)
        self.assertIn(f"0 <= {certificate.contradiction_sum}", text)

    def test_empty_offset_bounds_directly_infeasible(self):
        mapper = DateMapper(
            event_dates={1: {"a": 0}},
            offset_bounds={1: (3, 3)},
            proposed_offsets={1: 0},
            gap_constraints=[GapConstraint((1, "a"), (1, "a"), min_gap_days=1)],
        )
        with self.assertRaises(InfeasibleError) as caught:
            mapper.finalize()
        self.assertTrue(caught.exception.certificate.verify())

    def test_certificate_inequalities_are_actual_input_constraints(self):
        mapper = DateMapper(
            event_dates={1: {"a": 0}, 2: {"b": 10}},
            offset_bounds={1: (-5, 5), 2: (-5, 5)},
            proposed_offsets={1: 0, 2: 0},
            gap_constraints=[GapConstraint((1, "a"), (2, "b"), min_gap_days=1000)],
        )
        with self.assertRaises(InfeasibleError) as caught:
            mapper.finalize()
        origins = {
            item.origin for item in caught.exception.certificate.inequalities
        }
        # 每条证书不等式都能追溯到具体输入约束（边界或某条间隔约束）。
        self.assertTrue(all(origin is not None for origin in origins))
        self.assertTrue(
            all(
                origin[0] in ("bound", "gap")
                for origin in origins
                if isinstance(origin, tuple)
            )
        )


class BoundaryTests(unittest.TestCase):
    def test_no_persons_produces_empty_plan(self):
        mapper = DateMapper({}, {}, {})
        plan = mapper.finalize()
        self.assertEqual(plan.offsets, {})
        self.assertEqual(mapper.map_all(), {})
        self.assertEqual(mapper.max_change, 0)

    def test_person_with_no_gap_constraints_keeps_proposed_offset(self):
        mapper = DateMapper(
            event_dates={3: {"e": 9}},
            offset_bounds={3: (-4, 4)},
            proposed_offsets={3: 2},
        )
        mapper.finalize()
        self.assertEqual(mapper.offsets, {3: 2})
        self.assertEqual(mapper.max_change, 0)

    def test_proposed_outside_bounds_pulls_to_nearest_feasible(self):
        mapper = DateMapper(
            event_dates={1: {"e": 0}},
            offset_bounds={1: (1, 5)},
            proposed_offsets={1: -3},
        )
        mapper.finalize()
        self.assertEqual(mapper.offsets, {1: 1})
        self.assertEqual(mapper.max_change, 4)

    def test_singleton_bounds_pin_offset(self):
        mapper = DateMapper(
            event_dates={1: {"e": 0}, 2: {"e": 7}},
            offset_bounds={1: (2, 2), 2: (-1, -1)},
            proposed_offsets={1: 0, 2: 0},
        )
        mapper.finalize()
        self.assertEqual(mapper.offsets, {1: 2, 2: -1})

    def test_zero_width_gap_on_equal_dates_is_feasible(self):
        mapper = DateMapper(
            event_dates={1: {"a": 50}, 2: {"b": 50}},
            offset_bounds={1: (-3, 3), 2: (-3, 3)},
            proposed_offsets={1: 0, 2: 0},
            gap_constraints=[GapConstraint((1, "a"), (2, "b"), 0, 0)],
        )
        mapper.finalize()
        self.assertEqual(
            mapper.map_event_ordinal(1, "a"),
            mapper.map_event_ordinal(2, "b"),
        )
        self.assertEqual(mapper.max_change, 0)

    def test_solution_respects_offset_bounds(self):
        mapper = DateMapper(
            event_dates={1: {"a": 0}, 2: {"b": 20}},
            offset_bounds={1: (-2, 2), 2: (-2, 2)},
            proposed_offsets={1: 0, 2: 0},
            gap_constraints=[GapConstraint((1, "a"), (2, "b"), min_gap_days=30)],
        )
        with self.assertRaises(InfeasibleError):
            mapper.finalize()


class ValidationTests(unittest.TestCase):
    def _kwargs(self, **overrides):
        base = dict(
            event_dates={1: {"a": 0}},
            offset_bounds={1: (-1, 1)},
            proposed_offsets={1: 0},
            gap_constraints=(),
        )
        base.update(overrides)
        return base

    def test_missing_offset_bounds_rejected(self):
        with self.assertRaises(ValueError):
            DateMapper(
                event_dates={1: {"a": 0}},
                offset_bounds={},
                proposed_offsets={1: 0},
            )

    def test_missing_proposed_offset_rejected(self):
        with self.assertRaises(ValueError):
            DateMapper(
                event_dates={1: {"a": 0}},
                offset_bounds={1: (-1, 1)},
                proposed_offsets={},
            )

    def test_bound_for_unknown_person_rejected(self):
        with self.assertRaises(ValueError):
            DateMapper(
                event_dates={1: {"a": 0}},
                offset_bounds={1: (-1, 1), 2: (-1, 1)},
                proposed_offsets={1: 0},
            )

    def test_inverted_bounds_rejected(self):
        with self.assertRaises(ValueError):
            DateMapper(
                event_dates={1: {"a": 0}},
                offset_bounds={1: (2, 1)},
                proposed_offsets={1: 0},
            )

    def test_gap_referencing_unknown_event_rejected(self):
        with self.assertRaises(ValueError):
            DateMapper(
                **self._kwargs(
                    gap_constraints=[
                        GapConstraint((1, "a"), (1, "ghost"), min_gap_days=0)
                    ]
                )
            )

    def test_gap_needs_at_least_one_side(self):
        with self.assertRaises(ValueError):
            GapConstraint((1, "a"), (1, "a"))

    def test_gap_min_greater_than_max_rejected(self):
        with self.assertRaises(ValueError):
            GapConstraint((1, "a"), (1, "a"), min_gap_days=5, max_gap_days=2)

    def test_boolean_offset_rejected(self):
        with self.assertRaises(TypeError):
            DateMapper(
                event_dates={1: {"a": 0}},
                offset_bounds={1: (-1, 1)},
                proposed_offsets={1: True},
            )

    def test_bad_date_type_rejected(self):
        with self.assertRaises(TypeError):
            DateMapper(
                event_dates={1: {"a": "2020-01-01"}},
                offset_bounds={1: (-1, 1)},
                proposed_offsets={1: 0},
            )

    def test_mapping_before_finalize_rejected(self):
        mapper = DateMapper(
            event_dates={1: {"a": 0}},
            offset_bounds={1: (-1, 1)},
            proposed_offsets={1: 0},
        )
        with self.assertRaises(RuntimeError):
            mapper.map_event(1, "a")
        with self.assertRaises(RuntimeError):
            mapper.offsets


if __name__ == "__main__":
    unittest.main()
