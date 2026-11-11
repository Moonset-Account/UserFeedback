"""单位解析：仅实现本项目声明支持的受限单位子集。

支持的协议子集（单位）：
  质量：g（克）、mg（毫克）、kg（千克）
  质量分数：%（质量百分比）、g/g（克每克）、mg/g（毫克每克）
  无量纲比例：%

解析规则（子集内固定写法，不做通用单位推演）：
  1. 去除空白，大小写不敏感匹配；
  2. 规范化为标准单位 g / mg / kg / % / g/g / mg/g；
  3. 同量纲之间提供换算倍数（factor），factor 表示「1 个该单位等于多少标准基准单位」。
     质量基准单位为 g；质量分数基准单位为 g/g（% = 1e-2 g/g，mg/g = 1e-3 g/g）。

子集外的单位（如 lb、mol/L、ppm 等）一律抛 UnsupportedUnit，由检查层记为单位不兼容，
避免在语义未知的情况下静默换算。
"""

from __future__ import annotations

from dataclasses import dataclass


class UnsupportedUnit(Exception):
    """单位不在声明支持的协议子集内。"""


# 标准单位 -> 量纲
_DIMENSION: dict[str, str] = {
    "g": "mass",
    "mg": "mass",
    "kg": "mass",
    "%": "fraction",
    "g/g": "fraction",
    "mg/g": "fraction",
}

# 标准单位 -> 相对基准单位的倍数
_FACTOR: dict[str, float] = {
    "g": 1.0,
    "mg": 1e-3,
    "kg": 1e3,
    "%": 1e-2,
    "g/g": 1.0,
    "mg/g": 1e-3,
}

# 页面常见写法 -> 标准单位（受限别名表，仅覆盖样例源站可能出现的写法）
_ALIASES: dict[str, str] = {
    "g": "g",
    "克": "g",
    "克重": "g",
    "mg": "mg",
    "毫克": "mg",
    "kg": "kg",
    "千克": "kg",
    "%": "%",
    "％": "%",
    "wt%": "%",
    "wt％": "%",
    "质量分数": "%",
    "g/g": "g/g",
    "mg/g": "mg/g",
}


@dataclass(frozen=True)
class Unit:
    """规范化后的单位。"""

    canonical: str
    dimension: str
    factor: float

    @property
    def label(self) -> str:
        return self.canonical


def normalize(raw: str | None) -> Unit:
    """把页面上的单位文本规范化为 Unit；无法识别时抛 UnsupportedUnit。"""
    if raw is None:
        raise UnsupportedUnit("单位缺失")
    key = _strip(raw)
    if key not in _ALIASES:
        raise UnsupportedUnit(f"不在支持子集内的单位: {raw!r}")
    canonical = _ALIASES[key]
    return Unit(canonical=canonical, dimension=_DIMENSION[canonical], factor=_FACTOR[canonical])


def _strip(raw: str) -> str:
    text = raw.strip().lower()
    # 统一全角百分号、去除内部空白
    return text.replace("％", "%").replace(" ", "").replace(" ", "")


def is_same_dimension(left: Unit, right: Unit) -> bool:
    return left.dimension == right.dimension


def scale_ratio(observed: Unit, expected: Unit) -> float:
    """返回把 observed 单位下的数值换算为 expected 单位时的倍数。

    即 value_in_expected = value_in_observed * observed.factor / expected.factor。
    量纲不同时抛 UnsupportedUnit，拒绝跨量纲换算。
    """
    if not is_same_dimension(observed, expected):
        raise UnsupportedUnit(
            f"量纲不兼容，无法换算: {observed.canonical}({observed.dimension}) "
            f"vs {expected.canonical}({expected.dimension})"
        )
    return observed.factor / expected.factor


SUPPORTED_UNITS: tuple[str, ...] = tuple(sorted(_DIMENSION))
