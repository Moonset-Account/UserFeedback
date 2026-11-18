"""时间线组件验收测试（unittest，全部为本地合成夹具，无网络/无外部数据）。

覆盖题面的交叉约束：
1. 可接受结果：共同观测 + 绝对时间 + 重启分段下的时间范围与偏移估计；
2. 应被阻止/拒绝的结果：矛盾锚点、矛盾先后关系触发 ConflictError，且状态回滚；
3. 边界/交错场景：区间端点相触、区间交错的顺序未定、重启段独立性、
   新增锚点只重算相关连通分量。

运行：python -I -m unittest discover -s tests -v
"""

from __future__ import annotations

import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from timeline import (  # noqa: E402
    AbsoluteAnchor,
    ConflictError,
    Device,
    Ordering,
    Record,
    SegmentKey,
    SharedEventAnchor,
    TimelineEngine,
)

DELTA = 1e-9


def _close(a: float, b: float) -> bool:
    if math.isinf(a) or math.isinf(b):
        return a == b
    return abs(a - b) <= DELTA


class TimelineTestBase(unittest.TestCase):
    def assertRange(self, rng, lower, upper):  # noqa: N802 - unittest 风格
        self.assertTrue(
            _close(rng.lower, lower),
            f"lower 期望 {lower}，实得 {rng.lower}",
        )
        self.assertTrue(
            _close(rng.upper, upper),
            f"upper 期望 {upper}，实得 {rng.upper}",
        )

    def assertOffset(self, bounds, seg, lower, upper):  # noqa: N802
        lo, hi = bounds[seg]
        self.assertTrue(_close(lo, lower), f"{seg} 偏移下界 期望 {lower}，实得 {lo}")
        self.assertTrue(_close(hi, upper), f"{seg} 偏移上界 期望 {upper}，实得 {hi}")


class TestAcceptableTimeline(TimelineTestBase):
    """可接受结果：三类设备、共同观测、绝对锚点、明确先后同时成立。"""

    def _build_engine(self) -> TimelineEngine:
        eng = TimelineEngine()
        eng.add_device(Device("cam", kind="camera"))
        eng.add_device(Device("door", kind="access"))
        eng.add_device(Device("ws", kind="forensics"))

        # 摄像：本地 100 时真实约 120；误差 ±1
        eng.add_record(Record("cam1", "cam", 100.0, obs_error=1.0, source="cam.log:L1"))
        # 门禁：本地 50 时真实约 120；误差 ±2
        eng.add_record(Record("door1", "door", 50.0, obs_error=2.0, source="door.db:E1"))
        # 工作站：本地 200 时真实约 120；误差 0
        eng.add_record(Record("ws1", "ws", 200.0, obs_error=0.0, source="ws.export:R1"))
        # 工作站另有一条记录本地 205
        eng.add_record(Record("ws2", "ws", 205.0, obs_error=0.0, source="ws.export:R2"))

        # 共同观测事件 A1：cam1 与 ws1 同一真实时刻，设备间容差 1 秒
        eng.add_shared_anchor(
            SharedEventAnchor("A1", "cam1", "ws1", tolerance=1.0, source="event:A1")
        )
        # 共同观测事件 A0：door1 与 ws1 同一真实时刻，容差 1
        eng.add_shared_anchor(
            SharedEventAnchor("A0", "door1", "ws1", tolerance=1.0, source="event:A0")
        )
        # 绝对时间锚点：ws1 真实时间 = 120
        eng.add_absolute_anchor(
            AbsoluteAnchor("ABS1", "ws1", abs_ts=120.0, tolerance=0.0,
                           source="ntp:ABS1")
        )
        # 来源明确给出的先后：cam1 早于 ws2，至少间隔 2 秒
        eng.add_ordering(
            Ordering("cam1", "ws2", min_gap=2.0, source="chain-of-custody:1")
        )
        return eng

    def test_ranges_and_offsets(self) -> None:
        eng = self._build_engine()
        ranges = eng.time_ranges()

        # ws 段被绝对锚点钉死：偏移 = -80
        self.assertRange(ranges["ws1"], 120.0, 120.0)
        # ws2 同段单调：本地 205 => 真实 125；且被 min_gap=2 约束（自动满足）
        self.assertRange(ranges["ws2"], 125.0, 125.0)
        # cam1：与 ws1 严格同时 + 自身误差 ±1 => [119, 121]
        self.assertRange(ranges["cam1"], 119.0, 121.0)
        # door1：与 ws1(=120) 容差 1 => [119, 121]；自身误差 ±2 只收紧偏移
        self.assertRange(ranges["door1"], 119.0, 121.0)

        offsets = eng.offset_bounds()
        self.assertOffset(offsets, SegmentKey("ws", 0), -80.0, -80.0)
        # cam 偏移 = 真实 - 本地，cam1 真实 [119,121] => 偏移 [18,22]
        self.assertOffset(offsets, SegmentKey("cam", 0), 18.0, 22.0)
        # door 偏移：door1 ∈ [119,121]，obs ±2 => 偏移 ∈ [119-50-2, 121-50+2]=[67,73]
        self.assertOffset(offsets, SegmentKey("door", 0), 67.0, 73.0)

    def test_explicit_ordering_is_preserved_with_reason(self) -> None:
        eng = self._build_engine()
        result = eng.compare("cam1", "ws2")
        self.assertEqual(result.relation, "before")
        # 因果依据必须可溯源到明确先后关系
        self.assertIn("chain-of-custody:1", result.reasons)
        # 反向视角
        self.assertEqual(eng.compare("ws2", "cam1").relation, "after")

    def test_same_segment_monotone_is_causal(self) -> None:
        eng = self._build_engine()
        # ws1 -> ws2 同段本地时钟单调，即使点值也有因果依据
        result = eng.compare("ws1", "ws2")
        self.assertEqual(result.relation, "before")
        self.assertTrue(any("monotone" in r for r in result.reasons))

    def test_undetermined_pairs_only_overlapping_non_causal(self) -> None:
        eng = self._build_engine()
        undetermined = eng.undetermined_pairs()
        # cam1 [119,121] 与 door1 [117,123] 相交，且无因果链 => 顺序未定
        self.assertIn(("cam1", "door1"), undetermined)
        # door1 与 ws1 点值 120 相交、无因果链 => 顺序未定（端点相触也算相交）
        self.assertIn(("door1", "ws1"), undetermined)
        # 有明确先后的对不得出现
        self.assertNotIn(("cam1", "ws2"), undetermined)
        # 同段单调对不得出现
        self.assertNotIn(("ws1", "ws2"), undetermined)

        # compare 对顺序未定对返回 undetermined 且无依据
        res = eng.compare("cam1", "door1")
        self.assertEqual(res.relation, "undetermined")
        self.assertEqual(res.reasons, ())


class TestBoundaryAndInterleaving(TimelineTestBase):
    """边界与交错：端点相触、区间交错、无锚点无界。"""

    def test_touching_ranges_undetermined_without_cause(self) -> None:
        eng = TimelineEngine()
        eng.add_device(Device("d1"))
        eng.add_device(Device("d2"))
        # 两段偏移都无界；用绝对锚点人为制造端点相触的两个区间
        eng.add_record(Record("r1", "d1", 0.0, obs_error=1.0))
        eng.add_record(Record("r2", "d2", 10.0, obs_error=1.0))
        # r1 真实 <= 10（tolerance=1 且 abs=9 => [8,10]）
        eng.add_absolute_anchor(AbsoluteAnchor("B1", "r1", abs_ts=9.0, tolerance=1.0))
        # r2 真实 >= 10（abs=11, tol=1 => [10,12]）
        eng.add_absolute_anchor(AbsoluteAnchor("B2", "r2", abs_ts=11.0, tolerance=1.0))
        r1 = eng.time_range_of("r1")
        r2 = eng.time_range_of("r2")
        self.assertRange(r1, 8.0, 10.0)
        self.assertRange(r2, 10.0, 12.0)
        # 端点相触 + 无因果依据 => 顺序未定
        self.assertEqual(eng.compare("r1", "r2").relation, "undetermined")
        self.assertIn(("r1", "r2"), eng.undetermined_pairs())

        # 补一条明确先后（min_gap=0）后，同样的相触区间变成有因果的 before
        eng.add_ordering(Ordering("r1", "r2", min_gap=0.0, source="explicit:touch"))
        res = eng.compare("r1", "r2")
        self.assertEqual(res.relation, "before")
        self.assertIn("explicit:touch", res.reasons)
        self.assertNotIn(("r1", "r2"), eng.undetermined_pairs())

    def test_strictly_disjoint_ranges_are_ordered_by_range_evidence(self) -> None:
        eng = TimelineEngine()
        eng.add_device(Device("d1"))
        eng.add_device(Device("d2"))
        eng.add_record(Record("r1", "d1", 0.0, obs_error=0.5))
        eng.add_record(Record("r2", "d2", 0.0, obs_error=0.5))
        eng.add_absolute_anchor(AbsoluteAnchor("B1", "r1", abs_ts=10.0, tolerance=0.0))
        eng.add_absolute_anchor(AbsoluteAnchor("B2", "r2", abs_ts=20.0, tolerance=0.0))
        self.assertRange(eng.time_range_of("r1"), 10.0, 10.0)
        self.assertRange(eng.time_range_of("r2"), 20.0, 20.0)
        # 无因果链但区间严格分离 => 区间证据足以定序
        self.assertEqual(eng.compare("r1", "r2").relation, "before")
        self.assertEqual(eng.undetermined_pairs(), [])

    def test_interleaving_ranges_undetermined(self) -> None:
        eng = TimelineEngine()
        eng.add_device(Device("cam"))
        eng.add_device(Device("ws"))
        eng.add_record(Record("c1", "cam", 0.0, obs_error=2.0))
        eng.add_record(Record("w1", "ws", 0.0, obs_error=3.0))
        eng.add_absolute_anchor(AbsoluteAnchor("K1", "c1", abs_ts=100.0, tolerance=1.0))
        eng.add_absolute_anchor(AbsoluteAnchor("K2", "w1", abs_ts=101.0, tolerance=2.0))
        # c1 = 100±1 => [99,101]，w1 = 101±2 => [99,103]，交错且无因果依据
        self.assertRange(eng.time_range_of("c1"), 99.0, 101.0)
        self.assertRange(eng.time_range_of("w1"), 99.0, 103.0)
        self.assertEqual(eng.compare("c1", "w1").relation, "undetermined")
        self.assertIn(("c1", "w1"), eng.undetermined_pairs())

    def test_no_anchor_means_unbounded(self) -> None:
        eng = TimelineEngine()
        eng.add_device(Device("lonely"))
        eng.add_record(Record("x", "lonely", 42.0, obs_error=1.0))
        rng = eng.time_range_of("x")
        self.assertTrue(math.isinf(rng.lower) and math.isinf(rng.upper))
        seg, bounds = next(iter(eng.offset_bounds().items()))
        self.assertEqual(seg, SegmentKey("lonely", 0))
        self.assertTrue(math.isinf(bounds[0]) and math.isinf(bounds[1]))


class TestRebootSegments(TimelineTestBase):
    """重启把设备时钟切成独立段，段间偏移互不影响。"""

    def _build(self) -> TimelineEngine:
        eng = TimelineEngine()
        eng.add_device(Device("cam", kind="camera", reboots=[200.0]))
        eng.add_device(Device("ws", kind="forensics"))
        eng.add_record(Record("cam1", "cam", 100.0, obs_error=0.0))  # seg0
        eng.add_record(Record("cam2", "cam", 300.0, obs_error=0.0))  # seg1
        eng.add_record(Record("w1", "ws", 500.0, obs_error=0.0))
        # cam2（重启后）与 w1 共同观测；w1 绝对时间 1000
        eng.add_shared_anchor(SharedEventAnchor("R1", "cam2", "w1"))
        eng.add_absolute_anchor(AbsoluteAnchor("RABS", "w1", abs_ts=1000.0))
        return eng

    def test_segment_offsets_are_independent(self) -> None:
        eng = self._build()
        offsets = eng.offset_bounds()
        # ws 偏移 = 1000-500 = 500；cam seg1 偏移 = 1000-300 = 700
        self.assertOffset(offsets, SegmentKey("ws", 0), 500.0, 500.0)
        self.assertOffset(offsets, SegmentKey("cam", 1), 700.0, 700.0)
        # cam seg0 没有任何锚点 => 无界
        self.assertOffset(
            offsets, SegmentKey("cam", 0), -math.inf, math.inf
        )
        self.assertRange(eng.time_range_of("cam2"), 1000.0, 1000.0)
        rng1 = eng.time_range_of("cam1")
        self.assertTrue(math.isinf(rng1.lower) and math.isinf(rng1.upper))

    def test_records_across_reboot_have_no_monotone_causality(self) -> None:
        eng = self._build()
        # cam1(seg0) 与 cam2(seg1) 之间不存在同段单调因果，且 seg0 无界 => 未定
        self.assertEqual(eng.compare("cam1", "cam2").relation, "undetermined")
        # 同段内（这里每段仅一条记录）不受影响；显式先后可跨段建立因果
        eng.add_ordering(Ordering("cam1", "cam2", min_gap=0.0, source="reboot-log:1"))
        self.assertEqual(eng.compare("cam1", "cam2").relation, "before")

    def test_record_exactly_at_reboot_joins_new_segment(self) -> None:
        dev = Device("cam", reboots=[200.0])
        self.assertEqual(dev.segment_index_for(199.9), 0)
        self.assertEqual(dev.segment_index_for(200.0), 1)
        self.assertEqual(dev.segment_index_for(201.0), 1)


class TestConflictRejected(TimelineTestBase):
    """应被阻止/拒绝：矛盾锚点与矛盾先后关系，且引擎状态保持一致。"""

    def test_contradictory_absolute_anchors_raise_with_report(self) -> None:
        eng = TimelineEngine()
        eng.add_device(Device("cam"))
        eng.add_record(Record("c1", "cam", 100.0, obs_error=0.0))
        eng.add_absolute_anchor(AbsoluteAnchor("OK1", "c1", abs_ts=120.0))
        self.assertRange(eng.time_range_of("c1"), 120.0, 120.0)

        # 第二枚锚点要求 c1 真实时间 = 130，与 120 直接矛盾
        with self.assertRaises(ConflictError) as ctx:
            eng.add_absolute_anchor(
                AbsoluteAnchor("BAD1", "c1", abs_ts=130.0, source="bogus:BAD1")
            )
        report = ctx.exception.report
        # 冲突范围与源记录都要注明
        self.assertIn("OK1", report.conflicting_sources)
        self.assertIn("bogus:BAD1", report.conflicting_sources)
        self.assertIn(SegmentKey("cam", 0), report.conflicting_segments)
        # 隔离区间：OK1 => 偏移 20；BAD1 => 偏移 30；交集为空
        self.assertTrue(report.interval_clashes)
        clash = report.interval_clashes[0]
        self.assertEqual(clash.segment, SegmentKey("cam", 0))
        self.assertEqual({clash.anchor_a, clash.anchor_b}, {"OK1", "BAD1"})
        self.assertEqual(clash.interval_a, (20.0, 20.0))
        self.assertEqual(clash.interval_b, (30.0, 30.0))
        # 失败后引擎回滚到上一可行状态
        self.assertRange(eng.time_range_of("c1"), 120.0, 120.0)
        self.assertNotIn("BAD1", {a.anchor_id for a in eng._absolute_anchors})

    def test_contradictory_shared_anchor_raise_and_rollback(self) -> None:
        eng = TimelineEngine()
        eng.add_device(Device("cam"))
        eng.add_device(Device("ws"))
        eng.add_record(Record("c1", "cam", 100.0))
        eng.add_record(Record("w1", "ws", 100.0))
        eng.add_absolute_anchor(AbsoluteAnchor("P1", "c1", abs_ts=120.0))
        eng.add_absolute_anchor(AbsoluteAnchor("P2", "w1", abs_ts=180.0))
        # 共同观测要求 c1 与 w1 同时，但两者被钉在 120 与 180 => 矛盾
        with self.assertRaises(ConflictError) as ctx:
            eng.add_shared_anchor(
                SharedEventAnchor("S-BAD", "c1", "w1", source="event:S-BAD")
            )
        report = ctx.exception.report
        flat_sources = " ".join(report.conflicting_sources)
        self.assertIn("event:S-BAD", flat_sources)
        self.assertIn("P1", flat_sources)
        self.assertIn("P2", flat_sources)
        # 回滚：锚点未保留，原有可行解不变
        self.assertNotIn("S-BAD", {a.anchor_id for a in eng._shared_anchors})
        self.assertRange(eng.time_range_of("c1"), 120.0, 120.0)
        self.assertRange(eng.time_range_of("w1"), 180.0, 180.0)

    def test_contradictory_ordering_cycle_is_rejected(self) -> None:
        eng = TimelineEngine()
        eng.add_device(Device("d1"))
        eng.add_device(Device("d2"))
        eng.add_record(Record("a", "d1", 10.0))
        eng.add_record(Record("b", "d2", 10.0))
        eng.add_ordering(Ordering("a", "b", min_gap=5.0, source="doc:1"))
        self.assertEqual(eng.compare("a", "b").relation, "before")

        # 反向且要求 5 秒间隔，与 a < b（gap 5）构成负环
        with self.assertRaises(ConflictError) as ctx:
            eng.add_ordering(Ordering("b", "a", min_gap=5.0, source="doc:2"))
        sources = ctx.exception.report.conflicting_sources
        self.assertIn("doc:1", sources)
        self.assertIn("doc:2", sources)
        # 被拒排序未写入，旧结论保持
        self.assertEqual(eng.compare("a", "b").relation, "before")
        self.assertEqual(len(eng._orderings), 1)


class TestIncrementalRecompute(TimelineTestBase):
    """新增锚点只重算相关设备段（段连接图连通分量）。"""

    def _build(self) -> TimelineEngine:
        eng = TimelineEngine()
        eng.add_device(Device("cam", kind="camera", reboots=[200.0]))
        eng.add_device(Device("ws", kind="forensics"))
        eng.add_device(Device("door", kind="access"))
        # cam seg0 / seg1
        eng.add_record(Record("cam0", "cam", 100.0))
        eng.add_record(Record("cam1", "cam", 300.0))
        eng.add_record(Record("ws0", "ws", 500.0))
        eng.add_record(Record("door0", "door", 50.0))
        # 既有锚点只连接 cam seg1 与 ws
        eng.add_shared_anchor(SharedEventAnchor("A1", "cam1", "ws0"))
        eng.add_absolute_anchor(AbsoluteAnchor("ABS", "ws0", abs_ts=1000.0))
        return eng

    def test_new_anchor_only_recomputes_connected_component(self) -> None:
        eng = self._build()
        eng.solve()
        before = eng.time_ranges()

        # door 与 cam seg0 此前都是孤立分量；新锚点只把它们连起来
        eng.add_shared_anchor(SharedEventAnchor("A2", "cam0", "door0"))
        affected = eng.last_affected_segments()

        self.assertIn(SegmentKey("cam", 0), affected)
        self.assertIn(SegmentKey("door", 0), affected)
        # cam seg1 与 ws 属于另一连通分量，不应被重算
        self.assertNotIn(SegmentKey("cam", 1), affected)
        self.assertNotIn(SegmentKey("ws", 0), affected)

        # 未受影响记录的时间范围逐字段保持不变
        for untouched in ("cam1", "ws0"):
            old, new = before[untouched], eng.time_range_of(untouched)
            self.assertEqual(old.lower, new.lower)
            self.assertEqual(old.upper, new.upper)
            self.assertEqual(old.segment, new.segment)

        # 受影响记录确实被标记为重算
        recomputed = eng.last_recomputed_records()
        self.assertIn("cam0", recomputed)
        self.assertIn("door0", recomputed)
        self.assertNotIn("ws0", recomputed)

    def test_full_solve_marks_every_segment_affected(self) -> None:
        eng = self._build()
        eng.solve()
        self.assertEqual(
            eng.last_affected_segments(),
            {
                SegmentKey("cam", 0),
                SegmentKey("cam", 1),
                SegmentKey("ws", 0),
                SegmentKey("door", 0),
            },
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
