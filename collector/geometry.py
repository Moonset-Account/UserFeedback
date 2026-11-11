"""仿射变换矩阵与基础几何工具（纯标准库）。

约定矩阵为 6 元组 (a, b, c, d, e, f)，对应：
    | a c e |
    | b d f |
    | 0 0 1 |
"""

from __future__ import annotations

import math
from dataclasses import dataclass

IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def multiply(m1: tuple, m2: tuple) -> tuple:
    """返回 m1 * m2（先应用 m2，再应用 m1）。"""
    a1, b1, c1, d1, e1, f1 = m1
    a2, b2, c2, d2, e2, f2 = m2
    return (
        a1 * a2 + c1 * b2,
        b1 * a2 + d1 * b2,
        a1 * c2 + c1 * d2,
        b1 * c2 + d1 * d2,
        a1 * e2 + c1 * f2 + e1,
        b1 * e2 + d1 * f2 + f1,
    )


def translation(tx: float, ty: float) -> tuple:
    return (1.0, 0.0, 0.0, 1.0, float(tx), float(ty))


def scaling(sx: float, sy: float) -> tuple:
    return (float(sx), 0.0, 0.0, float(sy), 0.0, 0.0)


def rotation(deg: float, cx: float = 0.0, cy: float = 0.0) -> tuple:
    rad = math.radians(deg)
    cos, sin = math.cos(rad), math.sin(rad)
    rot = (cos, sin, -sin, cos, 0.0, 0.0)
    if cx == 0.0 and cy == 0.0:
        return rot
    # rotate(a cx cy) = translate(cx cy) rotate(a) translate(-cx -cy)
    return multiply(translation(cx, cy), multiply(rot, translation(-cx, -cy)))


def apply(m: tuple, x: float, y: float) -> tuple[float, float]:
    a, b, c, d, e, f = m
    return (a * x + c * y + e, b * x + d * y + f)


def invert(m: tuple) -> tuple:
    a, b, c, d, e, f = m
    det = a * d - b * c
    if abs(det) < 1e-18:
        raise ValueError("矩阵不可逆（退化为零面积）")
    inv_det = 1.0 / det
    return (
        d * inv_det,
        -b * inv_det,
        -c * inv_det,
        a * inv_det,
        (c * f - d * e) * inv_det,
        (b * e - a * f) * inv_det,
    )


def point_line_distance(px: float, py: float,
                        x1: float, y1: float, x2: float, y2: float) -> float:
    """点到无限长直线的距离。"""
    dx, dy = x2 - x1, y2 - y1
    denom = math.hypot(dx, dy)
    if denom < 1e-12:
        return math.hypot(px - x1, py - y1)
    return abs(dy * px - dx * py + x2 * y1 - y2 * x1) / denom


def segment_length(x1: float, y1: float, x2: float, y2: float) -> float:
    return math.hypot(x2 - x1, y2 - y1)


@dataclass
class Rect:
    x: float
    y: float
    width: float
    height: float

    def contains(self, px: float, py: float) -> bool:
        return (self.x <= px <= self.x + self.width
                and self.y <= py <= self.y + self.height)

    @property
    def corners(self) -> list[tuple[float, float]]:
        return [
            (self.x, self.y),
            (self.x + self.width, self.y),
            (self.x + self.width, self.y + self.height),
            (self.x, self.y + self.height),
        ]
