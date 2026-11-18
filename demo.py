"""端到端演示：摄像/门禁/取证工作站三设备时间线（本地合成数据）。

运行：python -I demo.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from timeline import (
    AbsoluteAnchor,
    ConflictError,
    Device,
    Ordering,
    Record,
    SharedEventAnchor,
    TimelineEngine,
)


def build() -> TimelineEngine:
    eng = TimelineEngine()
    # 摄像设备在本地 200 秒处重启过一次
    eng.add_device(Device("cam", kind="camera", reboots=[200.0]))
    eng.add_device(Device("door", kind="access"))
    eng.add_device(Device("ws", kind="forensics"))

    eng.add_record(Record("cam1", "cam", 100.0, obs_error=1.0, source="cam.log:L1"))
    eng.add_record(Record("cam2", "cam", 300.0, obs_error=1.0, source="cam.log:L9"))
    eng.add_record(Record("door1", "door", 50.0, obs_error=2.0, source="door.db:E1"))
    eng.add_record(Record("ws1", "ws", 200.0, obs_error=0.0, source="ws.export:R1"))
    eng.add_record(Record("ws2", "ws", 205.0, obs_error=0.0, source="ws.export:R2"))

    # 共同观测：cam1/door1 与 ws1 记录的是同一现场事件
    eng.add_shared_anchor(SharedEventAnchor("A1", "cam1", "ws1", tolerance=1.0))
    eng.add_shared_anchor(SharedEventAnchor("A0", "door1", "ws1", tolerance=1.0))
    # 取证工作站经 NTP 校准：ws1 真实时间 = 120
    eng.add_absolute_anchor(AbsoluteAnchor("ABS1", "ws1", abs_ts=120.0))
    # 来源记录明确给出 cam1 早于 ws2
    eng.add_ordering(Ordering("cam1", "ws2", min_gap=2.0, source="custody:1"))
    return eng


def main() -> None:
    eng = build()
    print("== 记录可能时间范围 ==")
    for rid, rng in sorted(eng.time_ranges().items()):
        print(f"  {rid:6s} [{rng.lower:>6}, {rng.upper:>6}]  段={rng.segment}")

    print("\n== 各重启段偏移可行区间 ==")
    for seg, (lo, hi) in sorted(eng.offset_bounds().items()):
        print(f"  {seg}: [{lo}, {hi}]")

    print("\n== 顺序判定 ==")
    for a, b in [("cam1", "ws2"), ("cam1", "door1"), ("ws1", "ws2")]:
        res = eng.compare(a, b)
        print(f"  {a} vs {b}: {res.relation}  依据={res.reasons}")
    print("  顺序未定对:", eng.undetermined_pairs())

    print("\n== 新增矛盾锚点的拒绝与冲突报告 ==")
    bad = eng
    try:
        bad.add_absolute_anchor(
            AbsoluteAnchor("BAD", "ws1", abs_ts=999.0, source="bogus:BAD")
        )
    except ConflictError as exc:
        rep = exc.report
        print("  已拒绝。message:", rep.message)
        print("  涉事来源:", rep.conflicting_sources)
        print("  涉事段:", rep.conflicting_segments)
        for clash in rep.interval_clashes:
            print("  区间冲突:", clash)
    print("  拒绝后 ws1 范围保持:", eng.time_range_of("ws1"))


if __name__ == "__main__":
    main()
