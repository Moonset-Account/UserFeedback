"""增量会话聚合算子使用的不可变值对象。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

DEFAULT_GAP_SECONDS = 10.0


@dataclass(frozen=True, slots=True)
class Event:
    """一条输入事件。

    ``event_id`` 在算子整个生命周期内唯一；``time`` 为事件时间（秒），
    允许为负；``amount`` 为参与求和的金额。到达顺序与事件时间无关。
    """

    event_id: str
    device: str
    time: float
    amount: float


@dataclass(frozen=True, slots=True, order=True)
class SessionResult:
    """对外发布的一个会话聚合结果。

    排序字段顺序固定为 ``device, first_time, last_time, event_count,
    amount_sum``，因此同一设备的多个结果可以直接用 ``sorted`` 得到稳定顺序。
    """

    device: str
    first_time: float
    last_time: float
    event_count: int
    amount_sum: float

    def __str__(self) -> str:  # pragma: no cover - 仅用于演示与断言失败信息
        return (
            f"{self.device}[{self.first_time:g}..{self.last_time:g}]"
            f" count={self.event_count} sum={self.amount_sum:g}"
        )


class ChangeKind(str, Enum):
    """changelog 的两种动作。"""

    INSERT = "insert"
    RETRACT = "retract"


@dataclass(frozen=True, slots=True)
class Change:
    """一条下游变更：旧结果的撤回或新结果的插入。"""

    kind: ChangeKind
    result: SessionResult

    @property
    def is_insert(self) -> bool:
        return self.kind is ChangeKind.INSERT

    @property
    def is_retract(self) -> bool:
        return self.kind is ChangeKind.RETRACT

    def __str__(self) -> str:  # pragma: no cover - 仅用于演示与断言失败信息
        return f"{self.kind.value} {self.result}"
