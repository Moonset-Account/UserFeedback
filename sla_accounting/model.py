"""领域模型：逻辑请求、重试跳、SLA 合同与滑动窗口口径。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .clocks import ObservedTime


class Outcome(str, Enum):
    """一次重试跳（attempt）的处理结果。"""

    SUCCESS = "success"
    FAILURE = "failure"  # 可用性失败（如 5xx、连接失败、切流拒绝）
    TIMEOUT = "timeout"  # 未在截止时间内得到应答


# 可用性失败类结果：超时在可用性口径下也算失败
AVAILABILITY_FAILURES = frozenset({Outcome.FAILURE, Outcome.TIMEOUT})


@dataclass(frozen=True)
class Attempt:
    """重试链上的一跳。

    - region:   该跳落地的区域（起点区或备用区）
    - observer: 上报该跳的观察者标识
    - start:    该跳发出时刻（带时钟误差）
    - finish:   该跳结算时刻（带时钟误差）；超时跳也有观测结算时刻
    - outcome:  处理结果
    - deadline: 该跳对应的截止时刻（备用区结算时刻口径，带时钟误差）
    """

    region: str
    observer: str
    start: ObservedTime
    finish: ObservedTime
    outcome: Outcome
    deadline: ObservedTime


@dataclass(frozen=True)
class LogicalRequest:
    """一次逻辑请求及其完整重试链。

    跨区域交接的两侧观测使用同一个 ``request_id``，从而保证一条逻辑
    请求在服务级预算里只计一次。

    - request_id:    全局唯一逻辑请求标识
    - service:       归属服务
    - origin_region: 起点区域（首跳所在区）
    - attempts:      按时间顺序排列的重试链，至少一跳
    - handed_off:    是否发生了跨区域交接（备用区接管）
    """

    request_id: str
    service: str
    origin_region: str
    attempts: tuple[Attempt, ...] = field(default_factory=tuple)
    handed_off: bool = False

    def __post_init__(self) -> None:
        if not self.request_id:
            raise ValueError("request_id 不能为空")
        if not self.service:
            raise ValueError("service 不能为空")
        if not self.attempts:
            raise ValueError(f"请求 {self.request_id} 至少需要一跳")
        regions = {a.region for a in self.attempts}
        if self.origin_region not in regions:
            raise ValueError(
                f"请求 {self.request_id} 的起点区 {self.origin_region} "
                f"不在重试链区域 {sorted(regions)} 中"
            )
        if self.handed_off and len(regions) < 2:
            raise ValueError(
                f"请求 {self.request_id} 标记为跨区交接，"
                f"但重试链只涉及 {sorted(regions)}"
            )

    @property
    def final_attempt(self) -> Attempt:
        return self.attempts[-1]

    @property
    def succeeded(self) -> bool:
        """一次最终成功的逻辑请求：重试链最后一跳成功。"""
        return self.final_attempt.outcome is Outcome.SUCCESS

    @property
    def regions(self) -> frozenset[str]:
        return frozenset(a.region for a in self.attempts)

    @property
    def observers(self) -> frozenset[str]:
        return frozenset(a.observer for a in self.attempts)


@dataclass(frozen=True)
class WindowSpec:
    """承诺的滑动窗口口径。

    - length_seconds: 窗口长度
    - error_budget:   窗口内容许的违约事件数（绝对值）
    - name:           窗口名称（如 short / long）
    """

    name: str
    length_seconds: float
    error_budget: int

    def __post_init__(self) -> None:
        if self.length_seconds <= 0:
            raise ValueError(f"窗口 {self.name} 长度必须为正")
        if self.error_budget < 0:
            raise ValueError(f"窗口 {self.name} 预算不能为负")


@dataclass(frozen=True)
class Contract:
    """服务合同（SLO/SLA 口径）。"""

    service: str
    windows: tuple[WindowSpec, ...]

    def __post_init__(self) -> None:
        if not self.windows:
            raise ValueError(f"服务 {self.service} 的合同至少需要一个窗口")
        names = [w.name for w in self.windows]
        if len(names) != len(set(names)):
            raise ValueError(f"服务 {self.service} 存在重名窗口: {names}")

    def window(self, name: str) -> WindowSpec:
        for w in self.windows:
            if w.name == name:
                return w
        raise KeyError(f"合同中不存在窗口 {name}")
