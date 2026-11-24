"""测试辅助：小值域上的暴力枚举参考实现。

通过枚举每个原子单元的样本在其整数取值上的所有分配，
列出满足全部观测的完整样本多重集，作为引擎结果的独立参照。
仅用于小规模测试（单元数与每单元样本数都很小）。
"""

from __future__ import annotations

from itertools import product
from typing import Dict, List, Sequence, Tuple

from quantile_merger.records import Observation


def atomic_cells(observations: Sequence[Observation]) -> List[Tuple[int, int]]:
    """由全部桶边界切出原子整数单元 (lo, hi)。"""
    first = observations[0]
    cuts = {first.lo - 1, first.hi}
    for o in observables(observations):
        for b in o.buckets:
            cuts.add(b.lo - 1)
            cuts.add(b.hi)
    pts = sorted(cuts)
    return [(pts[i] + 1, pts[i + 1]) for i in range(len(pts) - 1)]


def observables(observations: Sequence[Observation]):
    return observations


def cell_counts_of(observations: Sequence[Observation]) -> List[int]:
    """用前缀和等式直接解出每个原子单元的计数（覆盖型直方图下唯一）。"""
    first = observations[0]
    cuts = {first.lo - 1, first.hi}
    for o in observations:
        for b in o.buckets:
            cuts.add(b.lo - 1)
            cuts.add(b.hi)
    pts = sorted(cuts)
    # S(p) 由任一覆盖该区间的观测确定；逐点求解
    s_val: Dict[int, int] = {first.lo - 1: 0}
    remaining = set(pts[1:])
    # 每个桶给出 S(hi) = S(lo-1) + count，迭代直到全部求出
    while remaining:
        progressed = False
        for o in observations:
            for b in o.buckets:
                if b.lo - 1 in s_val and b.hi not in s_val:
                    s_val[b.hi] = s_val[b.lo - 1] + b.count
                    remaining.discard(b.hi)
                    progressed = True
        if not progressed:
            raise AssertionError("暴力参考实现无法解出单元计数")
    return [s_val[pts[i + 1]] - s_val[pts[i]] for i in range(len(pts) - 1)]


def enumerate_multisets(
    observations: Sequence[Observation],
) -> List[Tuple[int, ...]]:
    """枚举所有满足观测的样本多重集（排序后的元组形式）。"""
    first = observations[0]
    cells = atomic_cells(observations)
    counts = cell_counts_of(observations)

    # 校验所有观测确实被该单元计数满足（否则无可行解）
    for o in observations:
        for b in o.buckets:
            got = 0
            for (lo, hi), c in zip(cells, counts):
                a = max(lo, b.lo)
                z = min(hi, b.hi)
                if a <= z:
                    got += c  # 单元完全落在桶内（边界对齐）
            if got != b.count:
                return []

    # 每个单元 n 个样本分配到 w 个整数位置：组合数很小才枚举
    placements = []
    for (lo, hi), n in zip(cells, counts):
        width = hi - lo + 1
        options = []
        for splits in _weak_compositions(n, width):
            multiset = []
            for idx, cnt in enumerate(splits):
                multiset.extend([lo + idx] * cnt)
            options.append(tuple(multiset))
        placements.append(options)

    result = []
    for combo in product(*placements):
        merged = sorted(v for part in combo for v in part)
        result.append(tuple(merged))
    return result


def _weak_compositions(total: int, parts: int):
    """生成 total 拆成 parts 个非负整数的全部弱组合。"""
    if parts == 1:
        yield (total,)
        return
    for head in range(total + 1):
        for tail in _weak_compositions(total - head, parts - 1):
            yield (head,) + tail


def brute_quantile_bounds(
    observations: Sequence[Observation], k: int
) -> Tuple[int, int]:
    multisets = enumerate_multisets(observations)
    if not multisets:
        raise AssertionError("暴力枚举无可行解")
    kth_values = [ms[k - 1] for ms in multisets]
    return min(kth_values), max(kth_values)


def brute_window_bounds(
    per_segment_obs: Sequence[Sequence[Observation]], k: int
) -> Tuple[int, int]:
    """多段窗口的暴力极值：各段可行多重集独立枚举后做笛卡尔积。"""
    segment_choices = [enumerate_multisets(obs) for obs in per_segment_obs]
    if any(not choices for choices in segment_choices):
        raise AssertionError("暴力枚举存在不可行段")
    kth_values = []
    for combo in product(*segment_choices):
        merged = sorted(v for part in combo for v in part)
        kth_values.append(merged[k - 1])
    return min(kth_values), max(kth_values)
