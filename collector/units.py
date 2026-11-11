# -*- coding: utf-8 -*-
"""计量单位白名单校验（支持的协议子集：固定白名单，不做隐式单位换算）。"""
from __future__ import annotations

import re

# 别名 -> 规范单位。采集器只接受白名单内单位，未知单位标记为错误，
# 不猜测、不自动换算。
_UNIT_ALIASES: dict[str, str] = {
    # 浓度
    "mg/l": "mg/L",
    "ug/l": "μg/L",
    "μg/l": "μg/L",
    "µg/l": "μg/L",
    "mg/kg": "mg/kg",
    "ug/kg": "μg/kg",
    "μg/kg": "μg/kg",
    "µg/kg": "μg/kg",
    "mmol/l": "mmol/L",
    "mol/l": "mol/L",
    "ppm": "ppm",
    "ppb": "ppb",
    # 酸碱度 / 温度 / 压力
    "ph": "pH",
    "°c": "°C",
    "kpa": "kPa",
    "mpa": "MPa",
    # 长度 / 质量 / 容积 / 时间
    "m": "m",
    "cm": "cm",
    "mm": "mm",
    "kg": "kg",
    "g": "g",
    "mg": "mg",
    "ug": "μg",
    "μg": "μg",
    "µg": "μg",
    "l": "L",
    "ml": "mL",
    "ul": "μL",
    "μl": "μL",
    "µl": "μL",
    "s": "s",
    "ms": "ms",
    # 百分号
    "%": "%",
}

# 按长度降序排列，优先匹配长单位（mg/L 先于 mg、L）
_SORTED_ALIASES = sorted(_UNIT_ALIASES, key=len, reverse=True)


class UnitResult:
    def __init__(self, canonical: str | None, raw: str | None, error: str | None = None):
        self.canonical = canonical
        self.raw = raw
        self.error = error


def validate_unit(raw: str) -> UnitResult:
    """校验单位字符串，返回规范形式。"""
    if raw is None:
        return UnitResult(None, None)
    token = raw.strip()
    if not token:
        return UnitResult(None, None)
    key = token.lower()
    if key in _UNIT_ALIASES:
        return UnitResult(_UNIT_ALIASES[key], token)
    return UnitResult(
        None, token,
        f"非法/未知单位 {token!r}：不在受支持单位白名单内"
        f"（{', '.join(sorted(set(_UNIT_ALIASES.values())))}）",
    )


def split_unit(tail: str) -> tuple[str, str | None]:
    """从数值后的尾部文本中切出单位（允许空白分隔或直接连写）。

    返回 (剩余文本, 单位原文或 None)。匹配白名单别名（最长优先）。
    """
    text = tail.strip()
    if not text:
        return "", None
    low = text.lower()
    for alias in _SORTED_ALIASES:
        if low.endswith(alias):
            rest = text[: len(text) - len(alias)].strip()
            return rest, text[len(text) - len(alias):]
    # 未匹配到白名单单位：整段视为可疑单位文本，交由 validate_unit 报错
    return "", text


_WORD_RE = re.compile(r"[^a-zA-Zμµ°%/]+")
