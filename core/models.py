"""数据模型：rpa-order/v1 协议的内存表示（全部为虚构实验数据）。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class Step:
    id: str
    name: str
    group: Optional[str] = None


@dataclass
class Spec:
    """一份实验流程编辑页的步骤规约。"""

    scenario_id: str
    title: str
    steps: list[Step]
    # 固定锚点：步骤 id -> 固定下标（0 起）
    fixed: dict[str, int] = field(default_factory=dict)
    # 先后依赖：步骤 id -> 必须排在它前面的步骤 id 列表
    depends_on: dict[str, list[str]] = field(default_factory=dict)
    # 测试专用：执行到第 after_moves 次移动后模拟用户手动换位
    interference: Optional[dict] = None
    # 采集来源说明（http / file），用于证据展示
    source: str = ""

    @property
    def step_ids(self) -> list[str]:
        return [s.id for s in self.steps]

    def step(self, step_id: str) -> Step:
        for s in self.steps:
            if s.id == step_id:
                return s
        raise KeyError(step_id)
