"""执行器抽象。

协调模块只推导动作与结算回执，不真正触碰基础设施。执行器由调用方注入，
负责把动作送到真实/模拟执行环境，并在完成后把带代次（generation）的
结果回传给协调器。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class DispatchOrder:
    """一次派发指令，代次是协调器开出的栅栏令牌（fencing token）。"""

    action_id: str
    chain_id: str
    generation: int
    phase: str
    reads: tuple[str, ...]
    writes: tuple[str, ...]
    payload: dict[str, Any]


@runtime_checkable
class Executor(Protocol):
    """执行器需要实现的最小接口。

    实现方在回报时必须原样带回派发时的代次，协调器据此丢弃过期结果。
    """

    def dispatch(self, order: DispatchOrder) -> None: ...
