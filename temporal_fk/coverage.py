"""时态外键覆盖核对。

核对规则：对每个父键，取该键下**所有父记录有效期的并集**，要求该键下
每条子记录的整段半开有效期都被并集覆盖；子记录中任何未被覆盖的子区间
都构成违约。
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Sequence, Tuple

from .errors import ChildCoverageGap
from .intervals import merge_intervals, uncovered_parts


def evaluate_coverage(
    parents: Mapping[Any, Sequence[Tuple[Any, Any]]],
    children: Sequence[Tuple[int, Any, Tuple[Any, Any]]],
) -> List[ChildCoverageGap]:
    """基于给定的父 / 子关系快照计算覆盖缺口。

    :param parents: ``父键 -> 该键下所有父记录有效期列表``
    :param children: ``(子行 id, 父键, 子有效期)`` 三元组序列
    :returns: 违约子记录的缺口列表，为空表示约束成立
    """
    union_by_key: Dict[Any, List[Tuple[Any, Any]]] = {
        key: merge_intervals(list(intervals)) for key, intervals in parents.items()
    }

    gaps: List[ChildCoverageGap] = []
    for child_row_id, key, child_interval in children:
        cover = union_by_key.get(key, ())
        missing = uncovered_parts(child_interval, cover)
        if missing:
            gaps.append(
                ChildCoverageGap(
                    child_row_id=child_row_id,
                    key=key,
                    child_interval=child_interval,
                    missing=tuple(missing),
                )
            )
    return gaps
