"""可校对时钟源与观测时间区间。

每个时钟源相对于统一参考时钟存在一个可校对的误差范围 epsilon：
在该时钟上读数为 t 的事件，其真实发生时刻一定落在参考时钟的
闭区间 ``[t - epsilon, t + epsilon]`` 内。

因此两个观测时刻之间的先后关系、以及事件是否赶上截止时间，都可能
出现三态结论：确定成立、确定不成立、无法确定（可能违约）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Ternary(str, Enum):
    """带不确定性的三态判定结果。"""

    TRUE = "true"          # 确定成立（例如确定未违约）
    FALSE = "false"        # 确定不成立（例如确定违约）
    UNCERTAIN = "uncertain"  # 受时钟误差影响，两种情况都可能

    @property
    def is_certain(self) -> bool:
        return self is not Ternary.UNCERTAIN


@dataclass(frozen=True)
class ClockSource:
    """一个时钟源。

    ``epsilon`` 是该时钟相对参考时钟的可校对绝对误差（与读数同单位，
    通常为秒）：真实时刻 ∈ [读数 - epsilon, 读数 + epsilon]。
    """

    name: str
    epsilon: float

    def __post_init__(self) -> None:
        if self.epsilon < 0:
            raise ValueError(f"时钟误差不能为负: {self.name} epsilon={self.epsilon}")

    def observe(self, reading: float) -> "ObservedTime":
        """把该时钟上的一次读数转换为参考时钟上的时间区间。"""
        return ObservedTime(reading=reading, clock=self)


@dataclass(frozen=True)
class ObservedTime:
    """某个时钟源上的一次读数，以及它在参考时钟上对应的不确定区间。"""

    reading: float
    clock: ClockSource

    @property
    def lo(self) -> float:
        return self.reading - self.clock.epsilon

    @property
    def hi(self) -> float:
        return self.reading + self.clock.epsilon

    def as_interval(self) -> tuple[float, float]:
        return (self.lo, self.hi)


def compare(a: ObservedTime, b: ObservedTime) -> Ternary:
    """判定参考时钟上事件 a 是否早于/等于事件 b（a <= b）。

    - 若 a 的最大可能时刻仍不超过 b 的最小可能时刻 -> TRUE（确定 a 不晚于 b）
    - 若 a 的最小可能时刻也晚于 b 的最大可能时刻 -> FALSE（确定 a 晚于 b）
    - 两个区间有交集，先后无法确定 -> UNCERTAIN
    """
    if a.hi <= b.lo:
        return Ternary.TRUE
    if a.lo > b.hi:
        return Ternary.FALSE
    return Ternary.UNCERTAIN


def meets_deadline(event: ObservedTime, deadline: ObservedTime) -> Ternary:
    """事件是否在截止时间之前（含边界）完成，即 ``event <= deadline``。

    截止时间本身也来自某个时钟源的观测（例如备用区结算时刻），因此
    同样带误差区间。返回 TRUE 表示确定守约，FALSE 表示确定违约，
    UNCERTAIN 表示受时钟误差影响、可能违约。
    """
    return compare(event, deadline)


def interval_contains(interval: tuple[float, float], t: float) -> bool:
    """参考时钟时刻 t 是否落在闭区间内。"""
    lo, hi = interval
    return lo <= t <= hi


def interval_overlaps(
    interval: tuple[float, float], window: tuple[float, float]
) -> bool:
    """两个闭区间是否相交（含端点相接）。"""
    lo, hi = interval
    wlo, whi = window
    return lo <= whi and wlo <= hi
