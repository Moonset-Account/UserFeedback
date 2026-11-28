"""手算叙事用例：目标层级、字典序、日期运算、间隔上下界。"""

import datetime as dt
import unittest

import _bootstrap  # noqa: F401

from datemapper import DateMapper, GapConstraint, InfeasibleError


class NarrativeTests(unittest.TestCase):
    def test_child_sampled_before_birth_is_repaired(self):
        # p2 的采样日(105)早于 p1 的出生日(110)；发布后采样不得早于出生。
        mapper = DateMapper(
            event_dates={1: {"birth": 110}, 2: {"sample": 105}},
            offset_bounds={1: (-10, 10), 2: (-10, 10)},
            proposed_offsets={1: 0, 2: 0},
            gap_constraints=[GapConstraint((1, "birth"), (2, "sample"), min_gap_days=0)],
        )
        mapper.finalize()
        # 手算：x2 - x1 >= 5。D=3 时 x1 最小可行值为 -3（此时 x2=2）；
        # D=2 时 x1 >= -2 推出 x2 >= 3，但 |x2| <= 2，矛盾。
        self.assertEqual(mapper.offsets, {1: -3, 2: 2})
        self.assertEqual(mapper.max_change, 3)
        self.assertEqual(mapper.map_event_ordinal(1, "birth"), 107)
        self.assertEqual(mapper.map_event_ordinal(2, "sample"), 107)

    def test_lexicographic_tie_break_prefers_earlier_person_smaller_offset(self):
        # 同一最小 D 下存在多个解，字典序要求人 1 取最小可行值。
        mapper = DateMapper(
            event_dates={1: {"e": 0}, 2: {"e": 0}},
            offset_bounds={1: (-5, 5), 2: (-5, 5)},
            proposed_offsets={1: 0, 2: 0},
            gap_constraints=[GapConstraint((1, "e"), (2, "e"), min_gap_days=1)],
        )
        mapper.finalize()
        # 约束 x2 - x1 >= 1；最小 D=1，字典序最小解 x1=-1, x2=0。
        self.assertEqual(mapper.max_change, 1)
        self.assertEqual(mapper.offsets, {1: -1, 2: 0})

    def test_lex_order_follows_person_id_order_not_dict_insertion(self):
        # 输入字典故意按 2 再 1 插入，结果仍按人号 1、2 的顺序做字典序。
        mapper = DateMapper(
            event_dates={2: {"e": 0}, 1: {"e": 0}},
            offset_bounds={2: (-5, 5), 1: (-5, 5)},
            proposed_offsets={2: 0, 1: 0},
            gap_constraints=[GapConstraint((1, "e"), (2, "e"), min_gap_days=1)],
        )
        mapper.finalize()
        self.assertEqual(mapper.offsets, {2: 0, 1: -1})

    def test_proposed_offset_kept_when_already_feasible(self):
        mapper = DateMapper(
            event_dates={1: {"a": 100}, 2: {"b": 105}},
            offset_bounds={1: (-10, 10), 2: (-10, 10)},
            proposed_offsets={1: 0, 2: 0},
            gap_constraints=[GapConstraint((1, "a"), (2, "b"), min_gap_days=0)],
        )
        mapper.finalize()
        self.assertEqual(mapper.max_change, 0)
        self.assertEqual(mapper.offsets, {1: 0, 2: 0})

    def test_max_gap_constraint_bounds_published_distance(self):
        # 原始间隔 10 天，发布后间隔不得超过 4 天。
        mapper = DateMapper(
            event_dates={1: {"a": 100}, 2: {"b": 110}},
            offset_bounds={1: (-10, 10), 2: (-10, 10)},
            proposed_offsets={1: 0, 2: 0},
            gap_constraints=[GapConstraint((1, "a"), (2, "b"), max_gap_days=4)],
        )
        mapper.finalize()
        published_gap = (
            mapper.map_event_ordinal(2, "b") - mapper.map_event_ordinal(1, "a")
        )
        self.assertLessEqual(published_gap, 4)
        # 约束 x2 - x1 <= -6；最小 D=3，字典序最小解 x1=3, x2=-3（发布间隔 1）。
        self.assertEqual(mapper.offsets, {1: 3, 2: -3})
        self.assertEqual(mapper.max_change, 3)

    def test_min_and_max_gap_together_force_window(self):
        mapper = DateMapper(
            event_dates={1: {"a": 0}, 2: {"b": 0}},
            offset_bounds={1: (-9, 9), 2: (-9, 9)},
            proposed_offsets={1: 0, 2: 0},
            gap_constraints=[GapConstraint((1, "a"), (2, "b"), 2, 5)],
        )
        mapper.finalize()
        gap = mapper.map_event_ordinal(2, "b") - mapper.map_event_ordinal(1, "a")
        self.assertGreaterEqual(gap, 2)
        self.assertLessEqual(gap, 5)
        # 最小 D=1；字典序最小：x1 取最小可行 -1，此时 x2=1，间隔 2。
        self.assertEqual(mapper.offsets, {1: -1, 2: 1})

    def test_same_person_events_move_together(self):
        mapper = DateMapper(
            event_dates={1: {"birth": 100, "sample": 103}, 2: {"v": 200}},
            offset_bounds={1: (-10, 10), 2: (-10, 10)},
            proposed_offsets={1: 0, 2: 0},
            gap_constraints=[
                GapConstraint((1, "birth"), (1, "sample"), min_gap_days=0),
                GapConstraint((1, "sample"), (2, "v"), min_gap_days=0),
            ],
        )
        mapper.finalize()
        offset = mapper.offsets[1]
        # 同一人的两个事件使用同一个偏移，相互间隔保持 3 天不变。
        self.assertEqual(
            mapper.map_event_ordinal(1, "sample") - mapper.map_event_ordinal(1, "birth"),
            3,
        )
        self.assertEqual(mapper.map_event_ordinal(1, "birth"), 100 + offset)
        self.assertEqual(mapper.map_event_ordinal(1, "sample"), 103 + offset)

    def test_real_calendar_dates_round_trip(self):
        birth = dt.date(1990, 3, 1)
        sample = dt.date(1990, 2, 25)  # 采样早于出生
        mapper = DateMapper(
            event_dates={1: {"birth": birth}, 2: {"sample": sample}},
            offset_bounds={1: (-30, 30), 2: (-30, 30)},
            proposed_offsets={1: 0, 2: 0},
            gap_constraints=[GapConstraint((1, "birth"), (2, "sample"), min_gap_days=0)],
        )
        mapper.finalize()
        published_birth = mapper.map_event(1, "birth")
        published_sample = mapper.map_event(2, "sample")
        self.assertIsInstance(published_birth, dt.date)
        self.assertIsInstance(published_sample, dt.date)
        self.assertGreaterEqual((published_sample - published_birth).days, 0)
        self.assertEqual(
            published_birth,
            dt.date.fromordinal(birth.toordinal() + mapper.offsets[1]),
        )

    def test_integer_date_inputs_stay_integers(self):
        mapper = DateMapper(
            event_dates={7: {"e": 500}},
            offset_bounds={7: (-3, 3)},
            proposed_offsets={7: 1},
        )
        mapper.finalize()
        self.assertIsInstance(mapper.map_event(7, "e"), int)
        self.assertEqual(mapper.map_event(7, "e"), 501)

    def test_finalize_is_idempotent(self):
        mapper = DateMapper(
            event_dates={1: {"e": 0}},
            offset_bounds={1: (-2, 2)},
            proposed_offsets={1: 0},
        )
        first = mapper.finalize()
        second = mapper.finalize()
        self.assertEqual(first.offsets, second.offsets)
        self.assertEqual(mapper.offsets, {1: 0})


if __name__ == "__main__":
    unittest.main()
