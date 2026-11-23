"""测试用脚本执行器。

它不做任何真实修复，只记录派发/取消指令，方便测试驱动执行器回执。
同时模拟执行器"重启后仍持有旧代次在途任务"的场景。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .executor import DispatchOrder


@dataclass
class ScriptedExecutor:
    """记录所有收到的指令，测试可据此构造带代次的回执。"""

    dispatches: list[DispatchOrder] = field(default_factory=list)
    cancel_requests: list[tuple[str, int]] = field(default_factory=list)
    payloads: dict[str, dict[str, Any]] = field(default_factory=dict)
    started: bool = True

    def dispatch(self, order: DispatchOrder) -> None:
        if not self.started:
            raise RuntimeError("执行器尚未就绪（模拟重启窗口）")
        self.dispatches.append(order)

    def request_cancel(self, action_id: str, generation: int) -> None:
        self.cancel_requests.append((action_id, generation))

    def latest_generation(self, action_id: str) -> int:
        latest = 0
        for order in self.dispatches:
            if order.action_id == action_id:
                latest = max(latest, order.generation)
        return latest
