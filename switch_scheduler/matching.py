"""部分匹配枚举与匹配工具。"""

from __future__ import annotations

from functools import lru_cache

from .model import NO_PORT


def matching_is_valid(
    matching: tuple[int, ...], n_out: int, forbidden: frozenset[tuple[int, int]]
) -> bool:
    """检查匹配是否为合法部分匹配且不经过不可用交叉点。"""
    used: set[int] = set()
    for i, j in enumerate(matching):
        if j == NO_PORT:
            continue
        if not (0 <= j < n_out) or j in used or (i, j) in forbidden:
            return False
        used.add(j)
    return True


def changed_ports(
    prev: tuple[int, ...], new: tuple[int, ...]
) -> tuple[int, ...]:
    """目标匹配与上拍不同的输入端口（这些端口当拍不得发数）。"""
    return tuple(i for i, (a, b) in enumerate(zip(prev, new)) if a != b)


def active_edges(
    prev: tuple[int, ...], new: tuple[int, ...]
) -> tuple[tuple[int, int], ...]:
    """两拍保持不变的真实连线（本拍唯一可以发数的边）。"""
    return tuple(
        (i, j)
        for i, (a, j) in enumerate(zip(prev, new))
        if a == j and j != NO_PORT
    )


@lru_cache(maxsize=None)
def enumerate_matchings(
    n_in: int, n_out: int, allowed: tuple[tuple[int, ...], ...]
) -> tuple[tuple[int, ...], ...]:
    """枚举允许边上的全部部分匹配。

    allowed[i] 为输入 i 可连接的输出列表（须已排序、去重）。
    结果按匹配元组字典序排列；每个输入位先尝试悬空 (-1)，再按端口顺序尝试，
    因此 -1 排在最小，整体顺序确定，用于同价时按端口顺序固定结果。
    """
    results: list[tuple[int, ...]] = []
    current = [NO_PORT] * n_in
    used = [False] * n_out

    def backtrack(i: int) -> None:
        if i == n_in:
            results.append(tuple(current))
            return
        # 先尝试悬空：字典序中 -1 最小
        current[i] = NO_PORT
        backtrack(i + 1)
        for j in allowed[i]:
            if not used[j]:
                used[j] = True
                current[i] = j
                backtrack(i + 1)
                used[j] = False
        current[i] = NO_PORT

    backtrack(0)
    return tuple(results)


@lru_cache(maxsize=None)
def enumerate_active_matchings(
    n_in: int, n_out: int, allowed: tuple[tuple[int, ...], ...]
) -> tuple[tuple[int, ...], ...]:
    """仅在活跃输入（allowed 非空）上枚举部分匹配，非活跃位固定悬空。

    与 enumerate_matchings 给出同一集合（非活跃位本就只有悬空可选），
    但避免对恒悬空位做无效排列分支；结果仍按全长元组字典序排列。
    """
    active_idx = tuple(i for i in range(n_in) if allowed[i])
    if not active_idx:
        return (tuple([NO_PORT] * n_in),)
    sub_allowed = tuple(allowed[i] for i in active_idx)
    sub_results = enumerate_matchings(len(active_idx), n_out, sub_allowed)
    full: list[tuple[int, ...]] = []
    for sub in sub_results:
        m = [NO_PORT] * n_in
        for pos, i in enumerate(active_idx):
            m[i] = sub[pos]
        full.append(tuple(m))
    return tuple(full)


def allowed_outputs(
    problem,
    remaining: tuple[tuple[int, ...], ...],
    previous_matching: tuple[int, ...] | None = None,
) -> tuple[tuple[int, ...], ...]:
    """当前剩余需求下，每个输入允许建立目标连线的输出列表。

    包含两类边：
    1. 有剩余需求、且不在不可用交叉点上的边（建立无需求的新连线没有收益）；
    2. 当前匹配已占用的边，即使该边已无剩余需求——允许“保持现状不断开”，
       避免为了剔除零需求边而被迫产生一次无谓变更。
    """
    out: list[tuple[int, ...]] = []
    for i in range(problem.n_in):
        cols = {
            j
            for j in range(problem.n_out)
            if remaining[i][j] > 0 and (i, j) not in problem.forbidden
        }
        if previous_matching is not None:
            j = previous_matching[i]
            if j != NO_PORT and (i, j) not in problem.forbidden:
                cols.add(j)
        out.append(tuple(sorted(cols)))
    return tuple(out)
