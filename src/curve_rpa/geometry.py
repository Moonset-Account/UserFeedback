"""坐标系映射与曲线编辑器领域规则（纯函数，无浏览器依赖）。

页面采用“数据坐标 -> 屏幕像素”的仿射映射。RPA 不假设页面内部数值，
仅依据页面上**公开可见的刻度标签**反解映射（见 :mod:`curve_rpa.engine`），
本模块提供由刻度采样点拟合仿射映射、网格吸附、越界与顺序判定等通用数学工具，
同时被样例生成器与独立断言测试复用。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Sequence


def half_up_round(value: float) -> int:
    """与 JavaScript ``Math.round`` 一致的半值向上取整（银行家舍入会让网格正中间目标产生歧义）。"""
    return math.floor(value + 0.5)


@dataclass(frozen=True)
class TickSample:
    """一个公开刻度：tick_value 为刻度标签的数值，pixel 为其在页面上的像素坐标。"""

    tick_value: float
    pixel: float


@dataclass(frozen=True)
class AffineMap:
    """pixel = slope * value + intercept 的一维仿射映射。

    slope 为负表示坐标轴反向（例如反向纵轴：数值越大，像素越靠上）。
    """

    slope: float
    intercept: float

    def to_pixel(self, value: float) -> float:
        return self.slope * value + self.intercept

    def to_value(self, pixel: float) -> float:
        return (pixel - self.intercept) / self.slope


def fit_affine(samples: Sequence[TickSample]) -> AffineMap:
    """根据不少于 2 个公开刻度采样点最小二乘拟合仿射映射。

    刻度数值必须互不相同；页面渲染存在亚像素误差，用最小二乘而非两点定线，
    以削弱单个刻度定位误差的影响。
    """
    if len(samples) < 2:
        raise ValueError("拟合坐标映射至少需要 2 个刻度采样点")
    values = [s.tick_value for s in samples]
    if max(values) - min(values) == 0:
        raise ValueError("刻度采样点的数值不能全部相同")
    n = len(samples)
    mean_v = sum(values) / n
    mean_p = sum(s.pixel for s in samples) / n
    num = sum((s.tick_value - mean_v) * (s.pixel - mean_p) for s in samples)
    den = sum((s.tick_value - mean_v) ** 2 for s in samples)
    slope = num / den
    if slope == 0:
        raise ValueError("拟合结果斜率为 0，刻度像素坐标异常")
    intercept = mean_p - slope * mean_v
    return AffineMap(slope=slope, intercept=intercept)


def snap_to_grid(value: float, grid_step: float) -> float:
    """将数据值吸附到最近的网格线（等距网格，步长 grid_step）。

    半值采用向上吸附（与编辑器页面 JavaScript ``Math.round`` 完全一致），
    保证离线推导真值与浏览器实际行为不会在网格正中间目标上产生分歧。
    """
    if grid_step <= 0:
        raise ValueError("网格步长必须为正数")
    k = half_up_round(value / grid_step)
    return k * grid_step


def is_mid_grid(value: float, grid_step: float, tolerance: float = 1e-9) -> bool:
    """目标是否落在两条网格线之间（无法被精确吸附）。"""
    if grid_step <= 0:
        raise ValueError("网格步长必须为正数")
    nearest = snap_to_grid(value, grid_step)
    return abs(nearest - value) > tolerance


def out_of_bounds(value: float, low: float, high: float, tol: float = 1e-9) -> bool:
    """目标值是否超出轴域 [low, high]（含 tol 容差）。"""
    return value < low - tol or value > high + tol


def violates_x_order(targets: Iterable[float], tolerance: float = 1e-9) -> bool:
    """点顺序约束：控制点 x 必须严格递增，相等也视为违反（页面会阻止交叉）。"""
    prev: float | None = None
    for x in targets:
        if prev is not None and x <= prev + tolerance:
            return True
        prev = x
    return False


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def expected_snap_error(value: float, grid_step: float) -> float:
    """给定网格步长，该目标在页面上可达的最小绝对误差（网格吸附下界）。"""
    return abs(snap_to_grid(value, grid_step) - value)


def within_tolerance(actual: float, target: float, tolerance: float) -> bool:
    return abs(actual - target) <= tolerance + 1e-12
