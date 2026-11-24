"""跨时间段的窗口扫描：合并多个独立段的可行域，求窗口内第 k 小极值。

各时间段的样本互不流动，约束彼此独立；窗口查询等价于把各段的
前缀和极值函数逐点相加后，在全部原子边界上扫描。
"""

from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

from .model import (
    Allocation,
    KOutOfRangeError,
    SegmentModel,
    Witness,
)
from .records import Observation


def _window_prefix_bounds(
    models: Sequence[SegmentModel], v: int
) -> Tuple[int, int]:
    lo_sum = 0
    hi_sum = 0
    for m in models:
        if v <= m.anchor:
            # 落在该段值域左侧：前缀和为 0
            continue
        if v >= m.hi:
            lo_sum += m.total
            hi_sum += m.total
            continue
        a, b = m.prefix_bounds(v)
        lo_sum += a
        hi_sum += b
    return lo_sum, hi_sum


def _candidate_values(models: Sequence[SegmentModel]) -> List[int]:
    """所有可作为第 k 小取值的整数点：各原子单元的左右边界。"""
    vals = set()
    for m in models:
        # points[0] = anchor = lo-1；单元 i 为 (points[i], points[i+1]]
        for i in range(len(m.points) - 1):
            vals.add(m.points[i] + 1)  # 单元左边界（最小可取值）
            vals.add(m.points[i + 1])  # 单元右边界（最大可取值）
    return sorted(vals)


def compute_window_bounds(
    models: Sequence[SegmentModel], k: int
) -> Tuple[int, int]:
    """返回窗口合并样本第 k 小值的 (最小可能值, 最大可能值)。"""
    if not models:
        raise ValueError("窗口内没有任何段")
    total = sum(m.total for m in models)
    if not isinstance(k, int) or isinstance(k, bool) or k < 1 or k > total:
        raise KOutOfRangeError(f"k={k} 超出窗口样本总数 {total}")

    candidates = _candidate_values(models)

    # 最小 kth：最小的 v 使 max S(v) >= k
    min_kth = None
    for v in candidates:
        _, smax = _window_prefix_bounds(models, v)
        if smax >= k:
            min_kth = v
            break
    if min_kth is None:  # 理论上不可达：所有样本都在域内
        raise RuntimeError("内部错误：无法确定最小 kth")

    # 最大 kth：最大的 v 使 min S(v-1) <= k-1
    # （第 k 个样本可以放在 v，要求 v 之前严格少于 k 个样本）
    max_kth = None
    for v in reversed(candidates):
        smin_before, _ = _window_prefix_bounds(models, v - 1)
        if smin_before <= k - 1:
            max_kth = v
            break
    if max_kth is None:
        raise RuntimeError("内部错误：无法确定最大 kth")
    return min_kth, max_kth


def build_window_witness(
    models: Sequence[SegmentModel],
    k: int,
    bound: str,
    observations: Sequence[Observation],
    store_version: Optional[int],
    window_start: int,
    window_end: int,
    source: str,
    kth_value: int,
) -> Witness:
    """构造窗口级见证：每段独立取对应极值分配即可同时达到边界。"""
    allocations: List[Allocation] = []
    for m in models:
        allocations.append(m.build_allocation(bound))
    return Witness(
        source=source,
        window_start=window_start,
        window_end=window_end,
        k=k,
        bound=bound,
        kth_value=kth_value,
        allocations=tuple(allocations),
        observations=tuple(observations),
        store_version=store_version,
    )
