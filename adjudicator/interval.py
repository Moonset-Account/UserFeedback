"""带端点开闭标记的版本区间，支持无界区间与三态判定。

区间用 JSON 对象描述：

    {"lower": "1.0.0", "lower_inclusive": true,
     "upper": "2.0.0", "upper_inclusive": false}

端点规则：
- lower / upper 可省略或为 null，表示该侧无界：
  省略 lower 即 (-inf, ...)，省略 upper 即 (..., +inf)；
- lower_inclusive 缺省为 true（闭端，>=），false 为开端（>）；
- upper_inclusive 缺省为 false（开端，<），true 为闭端（<=）；
  这样的缺省值与 OSV/GHSA “introduced 闭、fixed 开” 的惯例一致；
- 端点本身必须可解析为三段式版本，否则该区间无法用于裁决，
  contains() 对任何输入都返回 UNKNOWN。

三态结果 IntervalState：
- INSIDE:  组件版本确定落在区间内（受影响）；
- OUTSIDE: 组件版本可解析且确定落在区间外；
- UNKNOWN: 组件版本或端点无法解析，无法比较，必须保留为“待核实”原因。
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from .versioning import IncomparableVersionError, Version, compare, parse_version


class IntervalState(str, Enum):
    INSIDE = "inside"
    OUTSIDE = "outside"
    UNKNOWN = "unknown"


class Interval:
    """单边或双边版本区间，端点开闭可配，任一侧均可无界。"""

    def __init__(
        self,
        lower: Optional[str] = None,
        upper: Optional[str] = None,
        lower_inclusive: bool = True,
        upper_inclusive: bool = False,
    ) -> None:
        self.lower_raw = lower
        self.upper_raw = upper
        self.lower_inclusive = bool(lower_inclusive)
        self.upper_inclusive = bool(upper_inclusive)
        self.lower: Optional[Version] = parse_version(lower) if lower is not None else None
        self.upper: Optional[Version] = parse_version(upper) if upper is not None else None
        # 记录配置了但解析失败的端点，供报告解释原因
        self.unparseable_endpoints = []
        if lower is not None and self.lower is None:
            self.unparseable_endpoints.append(("lower", lower))
        if upper is not None and self.upper is None:
            self.unparseable_endpoints.append(("upper", upper))
        if self.lower is not None and self.upper is not None:
            try:
                if compare(self.lower, self.upper) > 0:
                    raise ValueError(f"区间下界 {lower} 高于上界 {upper}")
            except IncomparableVersionError:
                # 理论上不会发生：两端都已解析成功
                raise ValueError(f"区间端点不可比较: {lower} ~ {upper}")

    @classmethod
    def from_dict(cls, data: dict) -> "Interval":
        return cls(
            lower=data.get("lower"),
            upper=data.get("upper"),
            lower_inclusive=bool(data.get("lower_inclusive", True)),
            upper_inclusive=bool(data.get("upper_inclusive", False)),
        )

    @property
    def well_formed(self) -> bool:
        """端点是否全部可解析。畸形区间不产生确定结论。"""
        return not self.unparseable_endpoints

    def contains(self, version: Optional[Version]) -> IntervalState:
        """三态成员判定。version 为 None（组件版本不可解析）时返回 UNKNOWN。"""
        if version is None or not self.well_formed:
            return IntervalState.UNKNOWN
        try:
            if self.lower is not None:
                lower_cmp = compare(version, self.lower)
                if lower_cmp < 0 or (lower_cmp == 0 and not self.lower_inclusive):
                    return IntervalState.OUTSIDE
            if self.upper is not None:
                upper_cmp = compare(version, self.upper)
                if upper_cmp > 0 or (upper_cmp == 0 and not self.upper_inclusive):
                    return IntervalState.OUTSIDE
            return IntervalState.INSIDE
        except IncomparableVersionError:
            return IntervalState.UNKNOWN

    def describe(self) -> str:
        left_bracket = "[" if self.lower_inclusive else "("
        right_bracket = "]" if self.upper_inclusive else ")"
        left = self.lower_raw if self.lower_raw is not None else "-inf"
        right = self.upper_raw if self.upper_raw is not None else "+inf"
        return f"{left_bracket}{left}, {right}{right_bracket}"
