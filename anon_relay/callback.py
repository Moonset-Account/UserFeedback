"""回拨解析。

来电只携带主叫号码与拨入的中继号。解析规则（半开区间，时间标量与
预约使用同一坐标系）：

1. 主叫不属于该中继号上任何预约的任一端 -> ``DENIED``：陌生主叫，
   结果不携带任何双方身份信息。
2. 主叫属于某条预约，且呼叫时刻落在其有效期 ``[start, end)`` 内：
   - 该中继号在该时刻对该主叫只有唯一一条有效期内的预约 ->
     ``CONNECTED``，返回唯一确定的另一端；
   - 同时存在多条可接通预约、无法唯一确定另一端 ->
     抛出 :class:`AmbiguousRoutingError`（合法分配下不应出现，出现即
     说明分配结果违反了冲突约束）。
3. 主叫的预约均已过有效期但仍处于冷却期 ``[end, end + cooldown)``：
   -> ``ENDED``：旧关系已结束，不得把通话转给中继号上的新联系人。
4. 冷却期也已过（或预约尚未开始）-> ``DENIED``：不泄露任何身份。

优先级：有效期内接通优先于冷却期结束判定。
"""

from __future__ import annotations

from typing import Sequence

from .models import Allocation, CallResult, CallStatus, Reservation


class AmbiguousRoutingError(Exception):
    """同一主叫在同一中继号、同一时刻存在多个可接通预约，无法唯一路由。"""


def _active(reservation: Reservation, at: int) -> bool:
    return reservation.start <= at < reservation.end


def _in_cooldown(reservation: Reservation, at: int) -> bool:
    return reservation.end <= at < reservation.occupied_end


def resolve_callback(
    caller: str,
    relay: str,
    at: int,
    reservations: Sequence[Reservation],
    allocation: Allocation,
) -> CallResult:
    """解析一次拨入中继号的来电。

    Args:
        caller: 主叫号码。
        relay: 拨入的中继号。
        at: 呼叫时刻（与预约时间同一坐标系的标量）。
        reservations: 系统中的全部预约。
        allocation: 中继号分配结果。

    Returns:
        :class:`CallResult`，状态为 ``CONNECTED`` / ``ENDED`` / ``DENIED``。
    """
    on_relay = [
        r
        for r in reservations
        if allocation.relay_of(r.reservation_id) == relay and r.involves(caller)
    ]

    # 陌生主叫：该中继号上没有任何预约与该号码相关。
    if not on_relay:
        return CallResult(status=CallStatus.DENIED, relay=relay, caller=caller)

    active = [r for r in on_relay if _active(r, at)]
    if len(active) > 1:
        raise AmbiguousRoutingError(
            f"主叫 {caller} 在 {at} 拨入中继号 {relay} 存在 "
            f"{len(active)} 条有效期内预约，无法唯一确定另一端: "
            f"{[r.reservation_id for r in active]}"
        )
    if len(active) == 1:
        reservation = active[0]
        other = reservation.callee if caller == reservation.caller else reservation.caller
        return CallResult(
            status=CallStatus.CONNECTED,
            relay=relay,
            caller=caller,
            other_party=other,
            reservation_id=reservation.reservation_id,
        )

    # 无有效期内预约：冷却期内的旧关系回拨 -> 已结束，禁止转到新联系人。
    cooling = [r for r in on_relay if _in_cooldown(r, at)]
    if cooling:
        # 同一主叫可能有多条冷却中的预约；取开始最晚者，行为对调用方
        # 而言都是 ENDED，且不暴露任何新联系人。
        latest = max(cooling, key=lambda r: (r.end, r.start))
        return CallResult(
            status=CallStatus.ENDED,
            relay=relay,
            caller=caller,
            reservation_id=latest.reservation_id,
        )

    # 预约尚未开始或冷却期已过：与陌生主叫同等处理，不泄露身份。
    return CallResult(status=CallStatus.DENIED, relay=relay, caller=caller)
