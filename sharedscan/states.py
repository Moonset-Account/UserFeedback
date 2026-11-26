"""可合并的聚合状态机。

共享扫描在每个分区内累积局部状态，在分区边界把局部状态并入全局状态。
状态种类：

* ``count_star`` / ``count`` / ``sum`` / ``min`` / ``max``：标准部分聚合状态，
  对不相交的行集合永远可以直接合并；
* ``distinct_set``：单分区内的去重值集合（仍保留具体值）；
* ``distinct_partial``：跨分区传递的去重计数**部分状态**，只保留计数、
  已经丢弃具体值。两个该状态能否相加取决于分区之间是否互斥——互斥时
  相加等价于集合并集的基数；不互斥时相加会重复计数，因此禁止合并。

AVG 没有专属状态：计划器把它拆成成对的 ``sum`` 与 ``count`` 状态，
投影阶段做 ``sum / count``，从而让多个 AVG / SUM / COUNT 请求共享同一批状态。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable

from .model import Scalar

# 状态种类常量
COUNT_STAR = "count_star"
COUNT = "count"
SUM = "sum"
MIN = "min"
MAX = "max"
DISTINCT_SET = "distinct_set"          # 局部：保留去重值集合
DISTINCT_PARTIAL = "distinct_partial"  # 跨分区：只保留计数


@dataclass(frozen=True)
class StateSpec:
    kind: str
    include_null: bool = True
    #: DISTINCT_PARTIAL 专用：为 True 时分区之间已标示互斥，计数可直接相加。
    exclusive: bool = False


@runtime_checkable
class State(Protocol):
    def add(self, value: Scalar) -> None: ...


# --------------------------------------------------------------------------- #
# 具体状态
# --------------------------------------------------------------------------- #
class _Counter:
    __slots__ = ("value",)

    def __init__(self) -> None:
        self.value: int = 0

    def add(self, value: Scalar) -> None:  # pragma: no cover - 由子类覆盖语义
        raise NotImplementedError


class CountStarState(_Counter):
    def add(self, value: Scalar) -> None:
        self.value += 1


class CountState(_Counter):
    def add(self, value: Scalar) -> None:
        if value is not None:
            self.value += 1


class SumState:
    __slots__ = ("value",)

    def __init__(self) -> None:
        self.value: Optional[int | float] = None

    def add(self, value: Scalar) -> None:
        if value is None:
            return
        if self.value is None:
            self.value = value
        else:
            self.value = self.value + value


class MinMaxState:
    __slots__ = ("value", "is_min")

    def __init__(self, is_min: bool) -> None:
        self.value: Scalar = None
        self.is_min = is_min

    def add(self, value: Scalar) -> None:
        if value is None:
            return
        if self.value is None or (value < self.value if self.is_min else value > self.value):
            self.value = value


class DistinctSetState:
    """单分区局部状态：保留去重后的具体值（含/不含 NULL 由 spec 决定）。"""

    __slots__ = ("values", "null_seen", "include_null")

    def __init__(self, include_null: bool) -> None:
        self.values: set = set()
        self.null_seen: bool = False
        self.include_null = include_null

    def add(self, value: Scalar) -> None:
        if value is None:
            if self.include_null:
                self.null_seen = True
            return
        self.values.add(value)

    def count(self) -> int:
        return len(self.values) + (1 if self.null_seen else 0)


class DistinctPartialState(_Counter):
    """跨分区部分状态：只有计数。仅在互斥分区下允许合并。"""


# --------------------------------------------------------------------------- #
# 状态生命周期
# --------------------------------------------------------------------------- #
def new_state(spec: StateSpec):
    if spec.kind == COUNT_STAR:
        return CountStarState()
    if spec.kind == COUNT:
        return CountState()
    if spec.kind == SUM:
        return SumState()
    if spec.kind == MIN:
        return MinMaxState(is_min=True)
    if spec.kind == MAX:
        return MinMaxState(is_min=False)
    if spec.kind == DISTINCT_SET:
        return DistinctSetState(spec.include_null)
    if spec.kind == DISTINCT_PARTIAL:
        return DistinctPartialState()
    raise ValueError(f"未知状态种类: {spec.kind!r}")


def update_state(state, value: Scalar) -> None:
    state.add(value)


def merge_states(spec: StateSpec, a, b):
    """合并两个在**不相交行集合**上累积的状态，返回新状态。"""
    if spec.kind in (COUNT_STAR, COUNT):
        out = type(a)()
        out.value = a.value + b.value
        return out
    if spec.kind == SUM:
        out = SumState()
        if a.value is None:
            out.value = b.value
        elif b.value is None:
            out.value = a.value
        else:
            out.value = a.value + b.value
        return out
    if spec.kind in (MIN, MAX):
        out = MinMaxState(is_min=(spec.kind == MIN))
        if a.value is None:
            out.value = b.value
        elif b.value is None:
            out.value = a.value
        else:
            out.value = min(a.value, b.value) if spec.kind == MIN else max(a.value, b.value)
        return out
    if spec.kind == DISTINCT_SET:
        # 单扫描内的集合并集，数学上总是正确（不经过部分计数状态）。
        out = DistinctSetState(spec.include_null)
        out.values = a.values | b.values
        out.null_seen = a.null_seen or b.null_seen
        return out
    if spec.kind == DISTINCT_PARTIAL:
        # 只保留了计数：只有互斥分区才能直接相加。计划器已保证不会在
        # 非互斥情形下构建这种合并；这里再做一次防御性拦截。
        raise InvalidDistinctMerge(
            "distinct_partial 状态只能在显式标示跨分区互斥时合并；"
            "非互斥去重计数必须保留值集合或改写请求"
        )
    raise ValueError(f"未知状态种类: {spec.kind!r}")


class InvalidDistinctMerge(RuntimeError):
    """非互斥分区上的去重计数部分状态被要求直接合并。"""


def finalize_state(spec: StateSpec, state) -> Scalar:
    """把状态收敛为输出标量。"""
    if spec.kind in (COUNT_STAR, COUNT, DISTINCT_PARTIAL):
        return state.value
    if spec.kind == DISTINCT_SET:
        return state.count()
    if spec.kind == SUM:
        return state.value  # 空组 -> NULL
    if spec.kind in (MIN, MAX):
        return state.value  # 空组 -> NULL
    raise ValueError(f"未知状态种类: {spec.kind!r}")


def empty_value(kind: str) -> Scalar:
    """零行组上该状态的空聚合值。"""
    if kind in (COUNT_STAR, COUNT, DISTINCT_SET, DISTINCT_PARTIAL):
        return 0
    if kind in (SUM, MIN, MAX):
        return None
    raise ValueError(f"未知状态种类: {kind!r}")


def avg_from_pair(sum_value: Scalar, count_value: Scalar) -> Optional[float]:
    """由成对的 sum/count 状态投影 AVG；count 为 0 时返回 NULL。"""
    if not count_value:
        return None
    return sum_value / count_value
