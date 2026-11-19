"""测试辅助：独立暴力枚举器，用于交叉验证生产代码。

与 ``publication.flow`` 的最大流实现完全独立——用带行/列合计剪枝的
回溯搜索枚举所有与可见值、上界、行列合计一致的整数表，再从枚举结果中
统计敏感格的取值区间并穷举全部候选子集的最优解。仅用于小规模夹具。
"""

from __future__ import annotations

from typing import Dict, FrozenSet, List, Sequence, Tuple

Pair = Tuple[int, int]


def enumerate_tables(
    table: Sequence[Sequence[int]],
    upper: Sequence[Sequence[int]],
    hidden: FrozenSet[Pair],
) -> List[Tuple[Tuple[int, ...], ...]]:
    """枚举与发布信息一致的全部可行表（回溯 + 行列剪枝）。"""
    rows = len(table)
    cols = len(table[0])
    row_sums = [sum(r) for r in table]
    col_sums = [sum(table[r][c] for r in range(rows)) for c in range(cols)]

    results: List[Tuple[Tuple[int, ...], ...]] = []
    grid = [[0] * cols for _ in range(rows)]

    def row_compositions(need: int, caps: Sequence[int]):
        """生成总和为 need、各分量不超过 caps 的全部非负整数行。"""
        if len(caps) == 0:
            if need == 0:
                yield ()
            return
        if len(caps) == 1:
            if need <= caps[0]:
                yield (need,)
            return
        head_max = min(need, caps[0])
        for head in range(head_max + 1):
            for tail in row_compositions(need - head, caps[1:]):
                yield (head,) + tail

    def dfs(r: int, col_used: List[int]) -> None:
        if r == rows:
            if col_used == col_sums:
                results.append(tuple(tuple(row) for row in grid))
            return

        # 可见格强制取真实值，先校验列余量并预占；只让隐藏格参与枚举。
        fixed_part = [0] * cols
        hidden_indexes: List[int] = []
        hidden_caps: List[int] = []
        need = row_sums[r]
        for c in range(cols):
            if (r, c) in hidden:
                cap = min(upper[r][c], col_sums[c] - col_used[c])
                if cap < 0:
                    return
                hidden_indexes.append(c)
                hidden_caps.append(cap)
            else:
                value = table[r][c]
                if value > col_sums[c] - col_used[c]:
                    return
                fixed_part[c] = value
                need -= value
        if need < 0:
            return

        for comp in row_compositions(need, hidden_caps):
            for c, value in zip(hidden_indexes, comp):
                fixed_part[c] = value
            for c, value in enumerate(fixed_part):
                if (r, c) not in hidden:
                    assert value == table[r][c]
                grid[r][c] = value
                col_used[c] += value
            dfs(r + 1, col_used)
            for c in range(cols):
                col_used[c] -= grid[r][c]
            for c in hidden_indexes:
                fixed_part[c] = 0

    dfs(0, [0] * cols)
    return results


def brute_ranges(
    table: Sequence[Sequence[int]],
    upper: Sequence[Sequence[int]],
    hidden: FrozenSet[Pair],
    cells: Sequence[Pair],
) -> Dict[Pair, Tuple[int, int]]:
    completions = enumerate_tables(table, upper, hidden)
    assert completions, "夹具本身必须可行（真实表即是一个解）"
    out: Dict[Pair, Tuple[int, int]] = {}
    for r, c in cells:
        values = {g[r][c] for g in completions}
        out[(r, c)] = (min(values), max(values))
    return out


def brute_best_subset(
    table: Sequence[Sequence[int]],
    upper: Sequence[Sequence[int]],
    sensitive: Sequence[Pair],
    candidates: Sequence[Pair],
    costs: Dict[Pair, int],
) -> Tuple[
    Tuple[Pair, ...] | None,
    int | None,
    Dict[Pair, Tuple[int, int]],
    Dict[Pair, Tuple[int, int]],
]:
    """穷举全部 2^k 个子集，独立求最优补隐藏方案。"""
    base = frozenset(sensitive)
    full_ranges = brute_ranges(table, upper, base | frozenset(candidates),
                               sensitive)

    best = None  # (cost, sorted tuple)
    best_ranges: Dict[Pair, Tuple[int, int]] = {}
    for mask in range(1 << len(candidates)):
        chosen = tuple(
            candidates[i] for i in range(len(candidates)) if mask & (1 << i)
        )
        hidden = base | frozenset(chosen)
        ranges = brute_ranges(table, upper, hidden, sensitive)
        if all(lo < hi for lo, hi in ranges.values()):
            key = (sum(costs[p] for p in chosen), chosen)
            if best is None or key < best:
                best = key
                best_ranges = ranges

    if best is None:
        return None, None, {}, full_ranges
    return best[1], best[0], best_ranges, full_ranges
