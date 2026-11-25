"""核心数据模型。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping, Optional


@dataclass(frozen=True)
class Reservation:
    """一条匿名通话预约。

    Attributes:
        reservation_id: 预约唯一标识。
        caller: 主叫号码（不透明字符串，按字面量比较）。
        callee: 被叫号码。
        start: 有效期起点（含），任意可比较的时间标量，如 Unix 时间戳。
        end: 有效期终点（不含，半开区间 ``[start, end)``）。
        cooldown: 回拨冷却时长，占用区间扩展为
            ``[start, end + cooldown)``，默认 0 表示无冷却。
        pinned_relay: 已对客户公布的预约固定使用的中继号；``None`` 表示
            未公布、可由分配器自由排号。
    """

    reservation_id: str
    caller: str
    callee: str
    start: int
    end: int
    cooldown: int = 0
    pinned_relay: Optional[str] = None

    @property
    def occupied_end(self) -> int:
        """占用区间终点（含冷却期），区间本身仍为半开。"""
        return self.end + self.cooldown

    def involves(self, number: str) -> bool:
        """该预约的任一端是否为给定号码。"""
        return number == self.caller or number == self.callee


class CallStatus(str, Enum):
    """回拨解析结果。

    CONNECTED: 唯一确定另一端并接通，结果中带双方号码与所属预约。
    ENDED:     号码在冷却期内回拨，原关系已结束，不得转到新联系人。
    DENIED:    陌生主叫（不属于该中继号上任何预约），不泄露任何身份信息。
    """

    CONNECTED = "connected"
    ENDED = "ended"
    DENIED = "denied"


@dataclass(frozen=True)
class CallResult:
    """一次回拨解析的结果。

    ``CONNECTED`` 时 ``other_party`` 与 ``reservation_id`` 有值；
    ``ENDED`` 时仅 ``reservation_id`` 有值（关系已结束，不给出新联系人）；
    ``DENIED`` 时两者均为 ``None``。
    """

    status: CallStatus
    relay: str
    caller: str
    other_party: Optional[str] = None
    reservation_id: Optional[str] = None

    @property
    def connected(self) -> bool:
        return self.status is CallStatus.CONNECTED


@dataclass(frozen=True)
class Allocation:
    """中继号分配结果。

    ``mapping`` 为预约 ID 到中继号的不可变映射；``relays`` 为实际使用的
    中继号集合。
    """

    mapping: Mapping[str, str]
    relays: frozenset[str]

    def relay_of(self, reservation_id: str) -> str:
        return self.mapping[reservation_id]

    def reservations_on(self, relay: str) -> tuple[str, ...]:
        """返回使用给定中继号的全部预约 ID（按注册顺序）。"""
        return tuple(rid for rid, r in self.mapping.items() if r == relay)

    @property
    def relay_count(self) -> int:
        return len(self.relays)
