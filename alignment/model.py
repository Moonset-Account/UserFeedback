"""数据模型:样本清单(版本要求)与压测运行记录。"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple

#: 清理事件中代表"资源已释放"的标记
RELEASE_EVENT = "resources_released"


class Expectation(Enum):
    """清单对样本的期望行为。"""

    COMPATIBLE = "compatible"  # 兼容样本:必须在限额内完成
    OVER_LIMIT = "over_limit"  # 超限样本:必须在规定阶段终止并释放资源


class Result(Enum):
    """压测设施上报的处理结果。"""

    COMPLETED = "completed"    # 正常完成
    TERMINATED = "terminated"  # 被终止(取消)
    INTERRUPTED = "interrupted"  # 计时被采集器中断,留待补证


@dataclass(frozen=True)
class SampleSpec:
    """版本要求的样本清单条目。预算按进程组合核对。"""

    sample_id: str
    category: str
    process_combo: Tuple[str, ...]
    expectation: Expectation
    memory_budget: int  # 该进程组合下的内存预算(峰值上限)
    required_stage: Optional[str] = None  # 超限样本必须终止的阶段

    def __post_init__(self) -> None:
        object.__setattr__(self, "process_combo", tuple(sorted(self.process_combo)))
        if self.memory_budget < 0:
            raise ValueError("memory_budget must be non-negative")
        if self.expectation is Expectation.OVER_LIMIT and not self.required_stage:
            raise ValueError("over_limit samples must declare required_stage")

    @property
    def key(self) -> Tuple[str, str, Tuple[str, ...]]:
        """同条件键:样本 + 输入类别 + 进程组合。"""
        return (self.sample_id, self.category, self.process_combo)


@dataclass(frozen=True)
class RunRecord:
    """压测设施给出的单条运行记录。"""

    sample_id: str
    category: str
    process_combo: Tuple[str, ...]
    result: Result
    limit_hit: bool                 # 是否命中限额
    memory_peak: int                # 内存峰值
    terminated_stage: Optional[str] = None  # 终止发生时的阶段
    cleanup_events: Tuple[str, ...] = ()    # 取消后的清理事件
    run_seq: int = 0                # 重跑序号(诊断用)

    def __post_init__(self) -> None:
        object.__setattr__(self, "process_combo", tuple(sorted(self.process_combo)))

    @property
    def key(self) -> Tuple[str, str, Tuple[str, ...]]:
        return (self.sample_id, self.category, self.process_combo)

    @property
    def resources_released(self) -> bool:
        """清理事件是否确认资源已释放。"""
        return RELEASE_EVENT in self.cleanup_events
