"""测试辅助：独立于求解器实现的逐拍穷举参考实现。

只用 Problem 的公开数据，自行枚举匹配与转移，按
(拍数, 变更总数, 匹配序列字典序) 求最优，用于交叉验证 solve()。
"""

from __future__ import annotations

from switch_scheduler.model import NO_PORT, Problem
from switch_scheduler.matching import active_edges, changed_ports


def _all_partial_matchings(n_in: int, n_out: int, allowed: list[list[int]]):
    res = []
    cur = [NO_PORT] * n_in
    used = [False] * n_out

    def dfs(i: int):
        if i == n_in:
            res.append(tuple(cur))
            return
        cur[i] = NO_PORT
        dfs(i + 1)
        for j in allowed[i]:
            if not used[j]:
                used[j] = True
                cur[i] = j
                dfs(i + 1)
                used[j] = False
        cur[i] = NO_PORT

    dfs(0)
    return res


def brute_force_optimal(problem: Problem, max_beats: int | None = None):
    """逐拍穷举，返回 (最优拍数, 最少变更, 最优匹配序列)；不可行返回 None。"""
    n_in, n_out = problem.n_in, problem.n_out
    if max_beats is None:
        max_beats = problem.default_window[1] + 1

    init_rem = tuple(x for row in problem.demand for x in row)
    # state: (remaining_flat, matching) -> (changes, path)
    level = {
        (init_rem, problem.previous_matching): (
            0,
            (problem.previous_matching,),
        )
    }
    for beat in range(max_beats + 1):
        goals = [v for (rem, _m), v in level.items() if sum(rem) == 0]
        if goals:
            changes, path = min(goals, key=lambda v: (v[0], v[1]))
            return beat, changes, path[1:]
        if beat == max_beats:
            break
        nxt = {}
        for (rem_flat, matching), (changes, path) in level.items():
            rem = [
                [rem_flat[i * n_out + j] for j in range(n_out)]
                for i in range(n_in)
            ]
            allowed = []
            for i in range(n_in):
                cols = {
                    j
                    for j in range(n_out)
                    if rem[i][j] > 0 and (i, j) not in problem.forbidden
                }
                # 允许保持当前已建立的连线，避免无谓变更
                j_now = matching[i]
                if j_now != NO_PORT and (i, j_now) not in problem.forbidden:
                    cols.add(j_now)
                allowed.append(sorted(cols))
            for m in _all_partial_matchings(n_in, n_out, allowed):
                ch = changed_ports(matching, m)
                new_rem = rem_flat
                sent_ok = True
                new_list = list(rem_flat)
                for i, j in active_edges(matching, m):
                    idx = i * n_out + j
                    if new_list[idx] <= 0:
                        continue
                    l, r = problem.window_of(i, j)
                    if not (l <= beat <= r):
                        continue
                    new_list[idx] -= 1
                new_flat = tuple(new_list)
                # 窗口错过剪枝（与求解器同一物理规则，独立书写）
                for i in range(n_in):
                    for j in range(n_out):
                        v = new_flat[i * n_out + j]
                        if v > 0:
                            l, r = problem.window_of(i, j)
                            if beat >= r:
                                sent_ok = False
                if not sent_ok:
                    continue
                key = (new_flat, m)
                cand = (changes + len(ch), path + (m,))
                if key not in nxt or cand < nxt[key]:
                    nxt[key] = cand
        level = nxt
    return None
