"""简化数据流事件模型：时钟读取、期限计算、保存、恢复、基准转换证明、比较。

每个事件都标注时钟来源（source）、单位（unit）与进程代次（generation）。
source 为 None 表示未标明来源，按规则进入待核对。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Union

# 可参与到期判定的有效时钟域
VALID_CLOCK_SOURCES = frozenset({"monotonic", "wall"})

# 单位到纳秒的换算表，用于统一单位
UNIT_TO_NS = {
    "ns": 1,
    "us": 1_000,
    "ms": 1_000_000,
    "s": 1_000_000_000,
}


@dataclass(frozen=True)
class ClockRead:
    """一次时钟读取。"""

    event_id: str
    source: Optional[str]  # "monotonic" / "wall" / None（未标明来源）
    value: int
    unit: Optional[str]
    generation: int  # 进程代次


@dataclass(frozen=True)
class DeadlineCompute:
    """以某个时间事件为基准加上时长，得到期限。"""

    event_id: str
    base: str  # 基准事件的 event_id（ClockRead 或 Restore）
    duration: int
    duration_unit: str


@dataclass(frozen=True)
class Save:
    """把期限保存下来（跨进程持久化）。"""

    event_id: str
    target: str  # DeadlineCompute 的 event_id
    generation: int


@dataclass(frozen=True)
class Restore:
    """在当前进程代次中恢复之前保存的期限。"""

    event_id: str
    saved: str  # Save 的 event_id
    generation: int  # 恢复所处的（当前）进程代次


@dataclass(frozen=True)
class BaselineProof:
    """基准转换证明：把 from_generation 的单调时钟基准换算到 to_generation。

    语义：old_generation_value = new_generation_value + offset（按 unit 计）。
    """

    event_id: str
    from_generation: int
    to_generation: int
    offset: int
    unit: str


@dataclass(frozen=True)
class Compare:
    """到期比较：deadline 与 now 两个时间点求差。"""

    event_id: str
    deadline: str  # DeadlineCompute 或 Restore 的 event_id
    now: str  # ClockRead 的 event_id


Event = Union[ClockRead, DeadlineCompute, Save, Restore, BaselineProof, Compare]
