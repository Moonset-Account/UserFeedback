"""算子对外暴露的拒绝类异常。

调用方可以捕获 :class:`RejectedOperation` 统一处理“输入被拒绝”的情况；
更细的异常类型用于区分拒绝原因。所有拒绝都是原子操作：抛出异常时
算子状态（水位、事件、已发布结果）保持不变。
"""

from __future__ import annotations


class SessionOperatorError(Exception):
    """算子相关错误的公共基类。"""


class RejectedOperation(SessionOperatorError):
    """输入违反流式约定，整条操作被拒绝且不产生任何 changelog。"""


class DuplicateEventId(RejectedOperation):
    """插入的事件编号已被使用过（编号在算子生命周期内唯一）。"""

    def __init__(self, event_id: str) -> None:
        super().__init__(f"事件编号 {event_id!r} 已被使用，插入被拒绝")
        self.event_id = event_id


class UnknownEventId(RejectedOperation):
    """按编号撤回时找不到对应事件（未插入或已被撤回）。"""

    def __init__(self, event_id: str) -> None:
        super().__init__(f"未知事件编号 {event_id!r}，撤回被拒绝")
        self.event_id = event_id


class WatermarkViolation(RejectedOperation):
    """事件时间在水位及以前，不允许再插入或撤回。"""

    def __init__(self, event_id: str, event_time: float) -> None:
        super().__init__(
            f"事件 {event_id!r} 的时间 {event_time!r} 已在水位及以前，操作被拒绝"
        )
        self.event_id = event_id
        self.event_time = event_time


class SealedSessionViolation(WatermarkViolation):
    """事件所属会话已封存，结果不可再变化。"""

    def __init__(self, event_id: str, event_time: float) -> None:
        super().__init__(event_id, event_time)
        self.args = (
            f"事件 {event_id!r} 所属会话已封存（时间 {event_time!r}），撤回被拒绝",
        )


class WatermarkRegression(RejectedOperation):
    """水位必须单调不减。"""

    def __init__(self, previous: float, attempted: float) -> None:
        super().__init__(
            f"水位不能回退：当前 {previous!r}，收到 {attempted!r}，推进被拒绝"
        )
        self.previous = previous
        self.attempted = attempted
