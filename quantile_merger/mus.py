"""极小矛盾观测子集（MUS）抽取。

题面要求的语义：找出一个矛盾观测子集，使得去掉其中任意一条后
剩余观测都能对应某个样本多重集。采用删除过滤（deletion-based）
算法：从完整集合出发，逐条尝试删除，若删除后仍矛盾则永久删除，
否则保留；最终保留下来的即为极小不可满足子集。
"""

from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

from .model import MusReport, SegmentModel
from .records import Observation


def is_feasible(observations: Sequence[Observation]) -> bool:
    """一组观测是否联合可行。空集视为可行。"""
    if not observations:
        return True
    try:
        model = SegmentModel(list(observations))
    except Exception:
        return False
    return model.feasible


def find_mus(observations: Sequence[Observation]) -> Optional[Tuple[Observation, ...]]:
    """返回极小矛盾子集；若整体可行则返回 None。"""
    if is_feasible(observations):
        return None
    current: List[Observation] = list(observations)
    # 删除过滤：保留每条"删了它仍然矛盾"的删除，留下的即极小集
    i = 0
    while i < len(current):
        candidate = current[:i] + current[i + 1 :]
        if not is_feasible(candidate):
            current = candidate  # 该条非必需，永久删除
        else:
            i += 1
    # 校验极小性：删任意一条都必须可行
    assert all(
        is_feasible(current[:j] + current[j + 1 :]) for j in range(len(current))
    )
    assert not is_feasible(current)
    return tuple(current)


def mus_report(observations: Sequence[Observation]) -> MusReport:
    """构造不可行段的完整报告（含 MUS 与矛盾原因）。"""
    mus = find_mus(observations)
    if mus is None:
        raise ValueError("该组观测可行，不存在 MUS")
    model = SegmentModel(list(observations))
    first = observations[0]
    return MusReport(
        source=first.source,
        segment_start=first.segment_start,
        segment_length=first.segment_length,
        mus=mus,
        reason=model.reason or "观测约束矛盾",
    )
