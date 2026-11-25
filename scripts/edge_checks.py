"""手工边界复核脚本（python3 scripts/edge_checks.py）。

不依赖任何第三方包与外部数据，仅用本地合成数据复核题面边界：
无冷却窗口、半开端点相接共号、14 条上限。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from anon_relay import (
    CallStatus,
    InvalidReservationError,
    RelayAllocator,
    Reservation,
    resolve_callback,
)


def main() -> None:
    # 1) 无冷却期：终点时刻立即回拨 -> DENIED，前一刻仍可接通。
    r = Reservation("X", "A", "B", 0, 10, cooldown=0)
    alloc = RelayAllocator([r]).allocate()
    assert (
        resolve_callback("A", alloc.relay_of("X"), 10, [r], alloc).status
        is CallStatus.DENIED
    )
    hit = resolve_callback("A", alloc.relay_of("X"), 9, [r], alloc)
    assert hit.status is CallStatus.CONNECTED and hit.other_party == "B"

    # 2) 半开端点相接可共号；冷却尾与新预约起点相同不冲突。
    r1 = Reservation("R1", "A", "B", 0, 10, cooldown=5)  # 占用 [0,15)
    r2 = Reservation("R2", "A", "C", 15, 20)             # 占用 [15,20)
    alloc2 = RelayAllocator([r1, r2]).allocate()
    assert alloc2.relay_count == 1, alloc2.mapping
    relay = alloc2.relay_of("R1")
    assert (
        resolve_callback("A", relay, 14, [r1, r2], alloc2).status
        is CallStatus.ENDED
    )
    got = resolve_callback("A", relay, 15, [r1, r2], alloc2)
    assert got.status is CallStatus.CONNECTED and got.other_party == "C"

    # 3) 14 条预约（同主叫、占区间完全重叠 -> 14 团）需要 14 个中继号。
    rs = [Reservation(f"R{i}", "S", f"Q{i}", 0, 10) for i in range(14)]
    assert RelayAllocator(rs).allocate().relay_count == 14

    # 3b) 同主叫但区间互不重叠时反而只需 1 个中继号。
    disjoint = [Reservation(f"D{i}", "S", f"Q{i}", i * 10, i * 10 + 5)
                for i in range(14)]
    assert RelayAllocator(disjoint).allocate().relay_count == 1

    # 4) 15 条超出上限，直接拒绝。
    try:
        RelayAllocator(
            [Reservation(f"R{i}", "S", f"Q{i}", i, i + 2) for i in range(15)]
        )
    except InvalidReservationError:
        pass
    else:  # pragma: no cover
        raise AssertionError("15 条预约必须被拒绝")

    print("edge checks OK")


if __name__ == "__main__":
    main()
