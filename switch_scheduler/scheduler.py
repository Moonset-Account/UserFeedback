"""核心求解器：逐层广度优先搜索最优拍序。

优化目标（按优先级）：
1. 拍数最少（精确清空需求矩阵）；
2. 同拍数下全帧连线变更端口次数最少；
3. 同价时按端口顺序（匹配元组字典序）固定唯一结果。

搜索状态 = (剩余需求扁平元组, 当前部分匹配)。一层即一拍；每拍枚举全部合法
目标匹配，未变更的连线才能发数，发数行为是确定的（能发且窗口允许即发）。

工程要点：
- 先用确定性贪心得到可行解上界 B，按 f = 当前拍 + 剩余下界 > B 剪枝；
- 节点只存父指针与“次序秩”，秩的生成顺序等价于匹配序列字典序，
  同状态按 (变更数, 秩) 取优，不再保存完整路径元组；
- 匹配对的变更端口/有效连线结果缓存，窗口拍号预计算为扁平数组。
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Optional

from .matching import (
    active_edges,
    allowed_outputs,
    changed_ports,
    enumerate_active_matchings,
    enumerate_matchings,
)
from .model import (
    BeatRecord,
    InfeasibleError,
    MissedDemand,
    Problem,
    Schedule,
)


# ---------------------------------------------------------------------------
# 下界
# ---------------------------------------------------------------------------

def lower_bounds(problem: Problem) -> dict:
    """行列最大负载等常用下界。

    任一输入/输出每拍至多参与一次传输，故拍数至少为最大行和与最大列和。
    它只是下界——重配延迟与业务窗口会使真实最优拍数更大。
    """
    row_load = [sum(row) for row in problem.demand]
    col_load = [
        sum(problem.demand[i][j] for i in range(problem.n_in))
        for j in range(problem.n_out)
    ]
    window_lb = 0
    for (i, j) in problem.demand_cells():
        l, r = problem.window_of(i, j)
        window_lb = max(window_lb, -(-problem.demand[i][j] // (r - l + 1)))
    return {
        "max_row_load": max(row_load, default=0),
        "max_col_load": max(col_load, default=0),
        "load_bound": max(max(row_load, default=0), max(col_load, default=0)),
        "window_bound": window_lb,
        "bound": max(
            max(row_load, default=0),
            max(col_load, default=0),
            window_lb,
        ),
    }


def _flatten(matrix: tuple[tuple[int, ...], ...]) -> tuple[int, ...]:
    return tuple(x for row in matrix for x in row)


def first_missed(
    problem: Problem,
    remaining: tuple[tuple[int, ...], ...],
    beat: int,
) -> Optional[MissedDemand]:
    """找出截至 beat（该拍已结束）首个已错过窗口的需求。"""
    best: Optional[tuple[int, int, int, MissedDemand]] = None
    for (i, j) in problem.demand_cells():
        l, r = problem.window_of(i, j)
        rem = remaining[i][j]
        if rem > 0 and beat >= r:
            cand = (r, i, j, MissedDemand(i, j, (l, r), rem, r))
            if best is None or cand[:3] < best[:3]:
                best = cand
    return None if best is None else best[3]


def _remaining_lower_bound_flat(
    flat: tuple[int, ...],
    n_in: int,
    n_out: int,
    l_arr: tuple[int, ...],
    r_arr: tuple[int, ...],
    row_of: tuple[int, ...],
    col_of: tuple[int, ...],
) -> int:
    """对扁平剩余需求计算拍数下界（行/列负载 + 每条边窗口容量）。"""
    row_sums = [0] * n_in
    col_sums = [0] * n_out
    bound = 0
    for idx in range(n_in * n_out):
        v = flat[idx]
        if v <= 0:
            continue
        row_sums[row_of[idx]] += v
        col_sums[col_of[idx]] += v
        b = -(-v // (r_arr[idx] - l_arr[idx] + 1))
        if b > bound:
            bound = b
    for v in row_sums:
        if v > bound:
            bound = v
    for v in col_sums:
        if v > bound:
            bound = v
    return bound


@lru_cache(maxsize=None)
def _diff_cached(prev: tuple[int, ...], new: tuple[int, ...]):
    return changed_ports(prev, new), active_edges(prev, new)


@dataclass
class _Node:
    remaining: tuple[int, ...]
    matching: tuple[int, ...]
    changes: int
    lower_bound: int
    path: tuple[tuple[int, ...], ...]  # 含初始匹配在内的完整匹配序列


# ---------------------------------------------------------------------------
# 贪心构造（给出上界与不可行诊断）
# ---------------------------------------------------------------------------

def greedy_schedule(
    problem: Problem,
):
    """确定性贪心：每拍选本拍发数最多（再按变更数、字典序）的安全匹配。

    返回 (匹配序列, 每拍发数边, 最终剩余矩阵)。贪心不保证最优，仅用于上界/诊断。
    “安全”指执行后不会有需求错过窗口。
    """
    n_in, n_out = problem.n_in, problem.n_out
    remaining = [list(row) for row in problem.demand]
    matching = problem.previous_matching
    path: list[tuple[int, ...]] = []
    sends_log: list[tuple[tuple[int, int], ...]] = []
    max_beat = problem.default_window[1] + 1
    beat = 0
    while any(any(row) for row in remaining) and beat <= max_beat:
        rem_tuple = tuple(tuple(row) for row in remaining)
        allowed = allowed_outputs(problem, rem_tuple, matching)
        matchings = enumerate_active_matchings(n_in, n_out, allowed)
        best_choice = None
        for m in matchings:
            changed, active = _diff_cached(matching, m)
            trial = [row[:] for row in remaining]
            sends: list[tuple[int, int]] = []
            for i, j in active:
                if trial[i][j] <= 0:
                    continue
                l, r = problem.window_of(i, j)
                if l <= beat <= r:
                    trial[i][j] -= 1
                    sends.append((i, j))
            trial_tuple = tuple(tuple(row) for row in trial)
            if first_missed(problem, trial_tuple, beat) is not None:
                continue
            # 前瞻：本拍目标匹配中仍有剩余需求的连线，下一拍可直接发
            # （冷启动建连拍 active 为空，需按目标匹配而非 active 统计）
            covered_with_demand = 0
            for i, j in enumerate(m):
                if j != -1 and trial[i][j] > 0:
                    covered_with_demand += 1
            key = (-len(sends), -covered_with_demand, len(changed), m)
            if best_choice is None or key < best_choice[0]:
                best_choice = (key, m, sends, trial)
        if best_choice is None:
            break
        _key, chosen, chosen_sends, remaining = best_choice
        path.append(chosen)
        sends_log.append(tuple(sorted(chosen_sends)))
        matching = chosen
        beat += 1
    return path, sends_log, tuple(tuple(row) for row in remaining)


# ---------------------------------------------------------------------------
# 记录重建
# ---------------------------------------------------------------------------

def _rebuild_records(
    problem: Problem, path: tuple[tuple[int, ...], ...]
) -> tuple[tuple[BeatRecord, ...], int]:
    """根据匹配序列确定性模拟，重建每拍可回放记录并统计变更数。"""
    remaining = [list(row) for row in problem.demand]
    prev = problem.previous_matching
    records: list[BeatRecord] = []
    total_changes = 0
    for beat, m in enumerate(path):
        changed, active = _diff_cached(prev, m)
        sends: list[tuple[int, int]] = []
        idle: list[tuple[int, int]] = []
        for i, j in active:
            if remaining[i][j] <= 0:
                continue
            l, r = problem.window_of(i, j)
            if l <= beat <= r:
                remaining[i][j] -= 1
                sends.append((i, j))
            else:
                idle.append((i, j))
        records.append(
            BeatRecord(
                beat=beat,
                matching=m,
                changed_ports=changed,
                active_edges=active,
                sends=tuple(sorted(sends)),
                idle_edges=tuple(sorted(idle)),
            )
        )
        total_changes += len(changed)
        prev = m
    if any(any(row) for row in remaining):
        raise RuntimeError("内部错误：重建记录后需求未清空")
    return tuple(records), total_changes


# ---------------------------------------------------------------------------
# 主求解
# ---------------------------------------------------------------------------

def solve(problem: Problem) -> Schedule:
    """求最短拍序；不可行时抛 InfeasibleError（带首个错过需求）。"""
    n_in, n_out = problem.n_in, problem.n_out
    if problem.total_demand() == 0:
        return Schedule(
            records=(),
            total_changes=0,
            demand=problem.demand,
            forbidden=problem.forbidden,
            windows=dict(problem.windows),
            initial_matching=problem.previous_matching,
        )

    n_cell = n_in * n_out
    default_l, default_r = problem.default_window
    l_arr = tuple(
        problem.windows.get(divmod(idx, n_out), (default_l, default_r))[0]
        for idx in range(n_cell)
    )
    r_arr = tuple(
        problem.windows.get(divmod(idx, n_out), (default_l, default_r))[1]
        for idx in range(n_cell)
    )
    row_of = tuple(idx // n_out for idx in range(n_cell))
    col_of = tuple(idx % n_out for idx in range(n_cell))
    # 每个 cell 的窗口容量（拍数），供下界的窗口项使用
    cap_arr = tuple(r_arr[idx] - l_arr[idx] + 1 for idx in range(n_cell))
    horizon = problem.default_window[1] + 1

    init_flat = _flatten(problem.demand)
    init_row_load = [0] * n_in
    init_col_load = [0] * n_out
    init_win_bound = 0
    for idx, v in enumerate(init_flat):
        if v > 0:
            init_row_load[row_of[idx]] += v
            init_col_load[col_of[idx]] += v
            wb = -(-v // cap_arr[idx])
            if wb > init_win_bound:
                init_win_bound = wb
    init_bound = max(
        max(init_row_load, default=0),
        max(init_col_load, default=0),
        init_win_bound,
    )
    if init_bound > horizon:
        raise InfeasibleError("不存在可行排程：窗口/容量下界超过时限", _diagnose(problem))

    # 贪心给出可行上界，用于 f 值剪枝
    greedy_path, _sends, greedy_left = greedy_schedule(problem)
    incumbent = len(greedy_path) if not any(any(row) for row in greedy_left) else None
    cap = incumbent if incumbent is not None else horizon

    root = _Node(
        remaining=init_flat,
        matching=problem.previous_matching,
        changes=0,
        lower_bound=init_bound,
        path=(problem.previous_matching,),
    )
    level: dict[tuple[tuple[int, ...], tuple[int, ...]], _Node] = {
        (init_flat, problem.previous_matching): root
    }
    beat = 0
    next_beat = beat + 1

    while level:
        # BFS：本层出现清空状态即达到最短拍数
        goals = [node for (flat, _m), node in level.items() if not any(flat)]
        if goals:
            # 先按变更数，再按完整匹配序列字典序，固定唯一最优结果
            best = min(goals, key=lambda n: (n.changes, n.path))
            records, total_changes = _rebuild_records(problem, best.path[1:])
            return Schedule(
                records=records,
                total_changes=total_changes,
                demand=problem.demand,
                forbidden=problem.forbidden,
                windows=dict(problem.windows),
                initial_matching=problem.previous_matching,
            )

        if beat >= cap:
            break

        next_level: dict[tuple[tuple[int, ...], tuple[int, ...]], _Node] = {}
        # 按完整路径字典序处理父节点，保证同状态首次写入者即字典序最优候选之一
        for node in sorted(level.values(), key=lambda n: n.path):
            flat = node.remaining
            if beat + node.lower_bound > cap:
                continue
            # 直接用扁平需求构造每个输入的可选输出，避免 deflatten
            allowed = _allowed_from_flat(
                problem, flat, n_in, n_out, node.matching
            )
            matchings = enumerate_active_matchings(n_in, n_out, allowed)
            for m in matchings:
                changed, active = _diff_cached(node.matching, m)
                sent_idx: list[int] = []
                if active:
                    work = list(flat)
                    for i, j in active:
                        idx = i * n_out + j
                        if work[idx] > 0 and l_arr[idx] <= beat <= r_arr[idx]:
                            work[idx] -= 1
                            sent_idx.append(idx)
                    new_flat = tuple(work)
                else:
                    new_flat = flat
                # 窗口可行性：父状态可行，仅本拍发送的 cell 剩余量改变，
                # 这些 cell 本拍窗口开放故 beat<r 必然成立；只需校验未来容量。
                feasible = True
                for idx in sent_idx:
                    v = new_flat[idx]
                    if v <= 0:
                        continue
                    left = r_arr[idx] - (next_beat if next_beat > l_arr[idx] else l_arr[idx]) + 1
                    if v > left:
                        feasible = False
                        break
                if not feasible:
                    continue
                key = (new_flat, m)
                new_changes = node.changes + len(changed)
                new_path = node.path + (m,)
                old = next_level.get(key)
                if old is None:
                    lb = _lower_bound_incremental(
                        flat, new_flat, sent_idx, n_in, n_out,
                        l_arr, r_arr, cap_arr, row_of, col_of,
                    )
                    next_level[key] = _Node(
                        remaining=new_flat,
                        matching=m,
                        changes=new_changes,
                        lower_bound=lb,
                        path=new_path,
                    )
                elif (new_changes, new_path) < (old.changes, old.path):
                    # 同状态取 (变更数, 完整路径字典序) 严格更优者
                    old.changes = new_changes
                    old.path = new_path
        level = next_level
        beat += 1
        next_beat = beat + 1

    raise InfeasibleError("不存在可精确清空矩阵的可行拍序", _diagnose(problem))


def _lower_bound_incremental(
    old_flat, new_flat, sent_idx, n_in, n_out,
    l_arr, r_arr, cap_arr, row_of, col_of,
) -> int:
    """下界增量计算：行/列负载只在发送 cell 上减少，窗口项只重算这些 cell。"""
    row_load = [0] * n_in
    col_load = [0] * n_out
    win_bound = 0
    sent_set = set(sent_idx)
    for idx in range(n_in * n_out):
        v = new_flat[idx]
        if v <= 0:
            continue
        row_load[row_of[idx]] += v
        col_load[col_of[idx]] += v
        if idx in sent_set:
            wb = -(-v // cap_arr[idx])
            if wb > win_bound:
                win_bound = wb
        else:
            # 未变化 cell 的窗口项与旧值相同，用旧剩余量直接算
            ov = old_flat[idx]
            wb = -(-ov // cap_arr[idx])
            if wb > win_bound:
                win_bound = wb
    bound = win_bound
    for v in row_load:
        if v > bound:
            bound = v
    for v in col_load:
        if v > bound:
            bound = v
    return bound


def _allowed_from_flat(
    problem: Problem,
    flat: tuple[int, ...],
    n_in: int,
    n_out: int,
    previous_matching: tuple[int, ...],
) -> tuple[tuple[int, ...], ...]:
    """扁平剩余需求版本的可选输出构造，避免矩阵 deflatten。"""
    out: list[tuple[int, ...]] = []
    forbidden = problem.forbidden
    for i in range(n_in):
        base = i * n_out
        cols = {
            j
            for j in range(n_out)
            if flat[base + j] > 0 and (i, j) not in forbidden
        }
        j = previous_matching[i]
        if j != -1 and (i, j) not in forbidden:
            cols.add(j)
        out.append(tuple(sorted(cols)))
    return tuple(out)


def _diagnose(problem: Problem) -> Optional[MissedDemand]:
    """搜索失败后用贪心模拟定位首个错过窗口的需求。"""
    _path, _sends, leftover = greedy_schedule(problem)
    earliest: Optional[MissedDemand] = None
    for (i, j) in problem.demand_cells():
        l, r = problem.window_of(i, j)
        rem = leftover[i][j]
        if rem > 0:
            cand = MissedDemand(i, j, (l, r), rem, r)
            if earliest is None or (r, i, j) < (
                earliest.missed_beat,
                earliest.i,
                earliest.j,
            ):
                earliest = cand
    if earliest is not None:
        return earliest
    cells = sorted(
        problem.demand_cells(),
        key=lambda c: (problem.window_of(*c)[1], c[0], c[1]),
    )
    if cells:
        i, j = cells[0]
        l, r = problem.window_of(i, j)
        return MissedDemand(i, j, (l, r), problem.demand[i][j], r)
    return None


def solve_safe(problem: Problem):
    """不抛异常版本：返回 (Schedule, None) 或 (None, InfeasibleError)。"""
    try:
        return solve(problem), None
    except InfeasibleError as exc:
        return None, exc
