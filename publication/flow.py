"""最大流（Dinic）与带上下界的运输可行性判定。

表的可行完成问题是一个二分运输网络：

* 源点 -> 行节点，容量为剩余行合计；
* 行节点 -> 列节点，容量为该格的自由取值区间；
* 列节点 -> 汇点，容量为剩余列合计。

固定为具体值 v 的格子：先把 v 从对应行列需求中预扣，不再建立该格
的边。带下界 lb 的格子同样先把 lb 预扣到行列需求上，边容量只保留
自由部分 ``high - low``。所有容量均为非负整数，判定最大流是否达到
剩余总需求即可。
"""

from __future__ import annotations

from collections import deque
from typing import List, Optional, Sequence, Tuple


class _Dinic:
    """整数容量网络上的 Dinic 最大流。"""

    def __init__(self, n: int) -> None:
        self.n = n
        self.graph: List[List[int]] = [[] for _ in range(n)]
        self.to: List[int] = []
        self.cap: List[int] = []

    def add_edge(self, u: int, v: int, cap: int) -> None:
        if cap < 0:
            raise ValueError("capacity must be non-negative")
        idx = len(self.to)
        self.to.append(v)
        self.cap.append(cap)
        self.graph[u].append(idx)
        self.to.append(u)
        self.cap.append(0)
        self.graph[v].append(idx + 1)

    def _bfs(self, s: int, t: int, level: List[int]) -> bool:
        for i in range(self.n):
            level[i] = -1
        level[s] = 0
        queue = deque([s])
        while queue:
            u = queue.popleft()
            for edge in self.graph[u]:
                if self.cap[edge] > 0 and level[self.to[edge]] < 0:
                    level[self.to[edge]] = level[u] + 1
                    queue.append(self.to[edge])
        return level[t] >= 0

    def _dfs(self, u: int, t: int, pushed: int, level: List[int],
             it: List[int]) -> int:
        if u == t:
            return pushed
        while it[u] < len(self.graph[u]):
            edge = self.graph[u][it[u]]
            v = self.to[edge]
            if self.cap[edge] > 0 and level[v] == level[u] + 1:
                flow = self._dfs(v, t, min(pushed, self.cap[edge]),
                                 level, it)
                if flow > 0:
                    self.cap[edge] -= flow
                    self.cap[edge ^ 1] += flow
                    return flow
            it[u] += 1
        return 0

    def max_flow(self, s: int, t: int) -> int:
        total = 0
        level = [-1] * self.n
        inf = 10**18
        while self._bfs(s, t, level):
            it = [0] * self.n
            while True:
                pushed = self._dfs(s, t, inf, level, it)
                if pushed == 0:
                    break
                total += pushed
        return total


def feasible(
    rows: int,
    cols: int,
    row_sums: Sequence[int],
    col_sums: Sequence[int],
    upper: Sequence[Sequence[int]],
    fixed: Optional[Sequence[Tuple[int, int, int]]] = None,
    clipped_lower: Optional[Sequence[Tuple[int, int, int]]] = None,
    clipped_upper: Optional[Sequence[Tuple[int, int, int]]] = None,
) -> bool:
    """判定是否存在与公开信息一致的非负整数表。

    参数
    ----
    rows, cols:
        表的行数、列数（均不超过 4）。
    row_sums, col_sums:
        必须公开的行、列合计。
    upper:
        每个格子的取值上界（含）。
    fixed:
        ``(r, c, v)`` 序列：该格被公开固定为 v（可见格或被探测的具体值）。
    clipped_lower:
        ``(r, c, lb)`` 序列：要求该格 >= lb（用于最小/最大值二分）。
    clipped_upper:
        ``(r, c, ub)`` 序列：要求该格 <= ub。

    同一格可同时带下界与上界；``fixed`` 与裁剪不应同时作用于同一格。
    """

    fixed_map = {}
    for r, c, v in fixed or ():
        if (r, c) in fixed_map and fixed_map[(r, c)] != v:
            return False
        fixed_map[(r, c)] = v

    low: List[List[int]] = [[0] * cols for _ in range(rows)]
    high: List[List[int]] = [
        [upper[r][c] for c in range(cols)] for r in range(rows)
    ]

    for r, c, lb in clipped_lower or ():
        if lb > high[r][c]:
            return False
        if lb > low[r][c]:
            low[r][c] = lb
    for r, c, ub in clipped_upper or ():
        if ub < low[r][c]:
            return False
        if ub < high[r][c]:
            high[r][c] = ub
    for (r, c), v in fixed_map.items():
        if v < low[r][c] or v > high[r][c]:
            return False

    rem_row = list(row_sums)
    rem_col = list(col_sums)
    for (r, c), v in fixed_map.items():
        rem_row[r] -= v
        rem_col[c] -= v
    if any(x < 0 for x in rem_row) or any(x < 0 for x in rem_col):
        return False

    # 下界部分同样预扣到行列需求上。
    for r in range(rows):
        for c in range(cols):
            if (r, c) in fixed_map:
                continue
            rem_row[r] -= low[r][c]
            rem_col[c] -= low[r][c]
    if any(x < 0 for x in rem_row) or any(x < 0 for x in rem_col):
        return False
    if sum(rem_row) != sum(rem_col):
        return False

    # 节点：0=源点；1..rows=行；rows+1..rows+cols=列；最后一个=汇点。
    source = 0
    sink = rows + cols + 1
    dinic = _Dinic(sink + 1)
    for r in range(rows):
        dinic.add_edge(source, 1 + r, rem_row[r])
    for c in range(cols):
        dinic.add_edge(1 + rows + c, sink, rem_col[c])

    for r in range(rows):
        for c in range(cols):
            if (r, c) in fixed_map:
                continue
            free_cap = high[r][c] - low[r][c]
            if free_cap < 0:
                return False
            if free_cap > 0:
                dinic.add_edge(1 + r, 1 + rows + c, free_cap)

    need = sum(rem_row)
    if need == 0:
        return True
    return dinic.max_flow(source, sink) == need
