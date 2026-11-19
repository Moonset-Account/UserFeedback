"""输入数据模型：原始观测与派生通告。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Observation:
    """一条原始观测。

    id:        观测编号（合成标识）。
    indicator: 该观测指向的合成指标编号。
    source:    原始采集源编号，独立性判定以它为最小粒度。
    """

    id: str
    indicator: str
    source: str


@dataclass(frozen=True)
class Advisory:
    """一条派生通告（对某指标给出一个方向的结论）。

    id:              通告编号。
    indicator:       结论指向的合成指标编号。
    direction:       结论方向（如 "malicious" / "benign"），不同方向互为相反结论。
    evidence_groups: 可替代的证据组；组与组之间是“或”，同一组内的材料缺一不可（“与”）。
                     组内元素为观测编号或其他通告编号（转引）。
    """

    id: str
    indicator: str
    direction: str
    evidence_groups: tuple[tuple[str, ...], ...]
