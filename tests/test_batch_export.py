"""同一份定稿计划跨批次、跨进程（序列化）导出的一致性。"""

import datetime as dt
import json
import unittest

import _bootstrap  # noqa: F401

from datemapper import DateMapper, GapConstraint


def _build_mapper():
    return DateMapper(
        event_dates={
            1: {"birth": dt.date(1985, 6, 1), "sample": dt.date(2010, 9, 9)},
            2: {"visit": dt.date(2010, 9, 1)},
            3: {"lab": 700000},
        },
        offset_bounds={1: (-40, 40), 2: (-40, 40), 3: (-40, 40)},
        proposed_offsets={1: 0, 2: 0, 3: 0},
        gap_constraints=[
            GapConstraint((1, "birth"), (1, "sample"), min_gap_days=0),
            GapConstraint((1, "sample"), (2, "visit"), min_gap_days=-3, max_gap_days=3),
        ],
    )


class BatchExportTests(unittest.TestCase):
    def test_repeated_calls_within_one_process_are_stable(self):
        mapper = _build_mapper()
        mapper.finalize()
        first = {
            (p, e): mapper.map_event(p, e)
            for p in (1, 2, 3)
            for e in mapper._event_ordinals[p]
        }
        # 模拟多批次重复导出。
        for _ in range(5):
            again = {
                (p, e): mapper.map_event(p, e)
                for p in (1, 2, 3)
                for e in mapper._event_ordinals[p]
            }
            self.assertEqual(again, first)

    def test_serialized_plan_reproduces_identical_dates(self):
        mapper = _build_mapper()
        mapper.finalize()
        expected = mapper.map_all()
        expected_offsets = mapper.offsets

        payload = json.dumps(mapper.to_plan_dict())  # 必须是 JSON 兼容的
        restored = DateMapper.from_plan_dict(json.loads(payload))

        self.assertEqual(restored.offsets, expected_offsets)
        self.assertEqual(restored.max_change, mapper.max_change)
        self.assertEqual(restored.map_all(), expected)

        # 分批查询：每一批单独问几个事件，结果与首批完全一致。
        batch_one = [restored.map_event(1, "birth"), restored.map_event(2, "visit")]
        batch_two = [
            restored.map_event(1, "birth"),
            restored.map_event(1, "sample"),
            restored.map_event(3, "lab"),
        ]
        self.assertEqual(batch_one[0], batch_two[0])
        self.assertEqual(batch_one[1], expected[2]["visit"])
        self.assertEqual(batch_two[2], expected[3]["lab"])

    def test_restored_plan_does_not_re_solve(self):
        mapper = _build_mapper()
        mapper.finalize()
        restored = DateMapper.from_plan_dict(json.loads(json.dumps(mapper.to_plan_dict())))
        # 已处于定稿状态，可直接映射；再调 finalize 也是幂等的。
        before = restored.map_event(1, "sample")
        restored.finalize()
        self.assertEqual(restored.map_event(1, "sample"), before)

    def test_plan_round_trip_preserves_date_kinds(self):
        mapper = _build_mapper()
        mapper.finalize()
        restored = DateMapper.from_plan_dict(mapper.to_plan_dict())
        self.assertIsInstance(restored.map_event(1, "birth"), dt.date)
        self.assertIsInstance(restored.map_event(3, "lab"), int)

    def test_unknown_event_raises_keyerror_after_finalize(self):
        mapper = _build_mapper()
        mapper.finalize()
        with self.assertRaises(KeyError):
            mapper.map_event(99, "nope")

    def test_unknown_plan_version_rejected(self):
        plan = _build_mapper().finalize()  # 先确保构造本身没问题
        del plan
        mapper = _build_mapper()
        mapper.finalize()
        payload = mapper.to_plan_dict()
        payload["format_version"] = 999
        with self.assertRaises(ValueError):
            DateMapper.from_plan_dict(payload)


if __name__ == "__main__":
    unittest.main()
