"""题面要求的演示：两个预约各有一端相同的排号与回拨结果。

场景（时间单位为分钟，半开区间）：

    预约 R1:  A(100) <-> B(200)，有效期 [0, 60)，  冷却 30
    预约 R2:  A(100) <-> C(300)，有效期 [60, 120)，冷却 30

R1 占用区间 [0, 90)，R2 占用区间 [60, 150)，二者在 [60, 90) 重叠且
共享号码 A，因此不能共用中继号，最少需要 2 个中继号：R1 -> R01，
R2 -> R02。回拨场景覆盖：正常接通、陌生主叫拒绝、冷却期旧主叫回拨
返回“已结束”且不泄露新联系人 C、冷却期结束后拒绝、半开区间边界。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from .allocator import RelayAllocator
from .callback import resolve_callback
from .models import Allocation, CallResult, CallStatus, Reservation

A = "100"
B = "200"
C = "300"
STRANGER = "999"


@dataclass(frozen=True)
class Scenario:
    description: str
    caller: str
    relay: str
    at: int
    expected_status: CallStatus
    expected_other: Optional[str]


def build_demo() -> tuple[list[Reservation], Allocation, list[Scenario]]:
    """构造演示预约、最少中继号分配与回拨场景（含期望值）。"""
    reservations = [
        Reservation(
            reservation_id="R1",
            caller=A,
            callee=B,
            start=0,
            end=60,
            cooldown=30,
        ),
        Reservation(
            reservation_id="R2",
            caller=A,
            callee=C,
            start=60,
            end=120,
            cooldown=30,
        ),
    ]
    allocation = RelayAllocator(reservations).allocate()
    relay1 = allocation.relay_of("R1")
    relay2 = allocation.relay_of("R2")

    scenarios = [
        Scenario("有效期内 A 拨 R01，唯一接通 B", A, relay1, 30,
                 CallStatus.CONNECTED, B),
        Scenario("有效期内 B 拨 R01，唯一接通 A", B, relay1, 30,
                 CallStatus.CONNECTED, A),
        Scenario("陌生主叫拨 R01，拒绝且不泄露身份", STRANGER, relay1, 30,
                 CallStatus.DENIED, None),
        Scenario("边界 t=60：A 拨 R02，R2 刚好生效，接通 C", A, relay2, 60,
                 CallStatus.CONNECTED, C),
        Scenario("边界 t=60：A 拨旧号 R01，R1 已结束进入冷却", A, relay1, 60,
                 CallStatus.ENDED, None),
        Scenario("冷却期内旧主叫 A 拨 R01，返回已结束，不得转给 C", A, relay1, 70,
                 CallStatus.ENDED, None),
        Scenario("冷却期内旧被叫 B 拨 R01，同样返回已结束", B, relay1, 75,
                 CallStatus.ENDED, None),
        Scenario("陌生主叫在冷却时段拨 R01，仍只得到拒绝", STRANGER, relay1, 70,
                 CallStatus.DENIED, None),
        Scenario("R2 有效期内陌生主叫拨 R02，拒绝", STRANGER, relay2, 90,
                 CallStatus.DENIED, None),
        Scenario("冷却期结束后 B 再拨 R01，拒绝且不泄露身份", B, relay1, 100,
                 CallStatus.DENIED, None),
    ]
    return reservations, allocation, scenarios


def run_demo() -> Sequence[CallResult]:
    """执行演示，打印排号与每条回拨结果，返回全部结果。"""
    reservations, allocation, scenarios = build_demo()

    print("=== 预约与占用区间（含冷却期）===")
    for r in reservations:
        print(
            f"  {r.reservation_id}: {r.caller} <-> {r.callee} "
            f"有效期[{r.start}, {r.end}) 冷却{r.cooldown} "
            f"占用[{r.start}, {r.occupied_end})"
        )
    print("=== 最少中继号分配 ===")
    for rid, relay in allocation.mapping.items():
        print(f"  {rid} -> {relay}")
    print(f"  使用中继号数量: {allocation.relay_count}")

    print("=== 回拨解析 ===")
    results: list[CallResult] = []
    for s in scenarios:
        result = resolve_callback(
            s.caller, s.relay, s.at, reservations, allocation
        )
        results.append(result)
        detail = result.status.value
        if result.other_party is not None:
            detail += f" -> 对端 {result.other_party}"
        if result.reservation_id is not None:
            detail += f" (预约 {result.reservation_id})"
        print(f"  t={s.at:>3} {s.caller} 拨 {s.relay}: {detail}")
        print(f"        期望 {s.expected_status.value}: {s.description}")
    return results


if __name__ == "__main__":  # pragma: no cover
    run_demo()
