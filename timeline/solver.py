"""差分约束系统求解器。

把所有约束写成 x_v - x_u <= w 的形式，边 u -> v 权重 w。
- 上界：从参考变量 REF（固定 0）出发的最短路径（仅 REF 初始为 0，其余为 +inf，
  因此与 REF 不连通的变量保持无界，而不会被错误地压到 0 以下）。
- 下界：在反向图上跑同样的最短路。
- 可行性：用“超级源”初始化（所有距离为 0）跑 n 轮，仍可松弛即存在负环。
负环上的边标签用于溯源。
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Tag:
    """约束边的溯源标签。

    kind: anchor(共同观测/绝对锚点) | ordering(明确先后) |
          monotone(同段本地时钟单调) | record(观测误差)
    """

    kind: str
    source: str

    def __str__(self) -> str:  # pragma: no cover - 报告用
        return f"{self.kind}:{self.source}"


# 前驱记录：(前驱节点, 边权重, 边标签)
PredEdge = tuple[int, float, Tag]


@dataclass
class NegativeCycle:
    edges: tuple[PredEdge, ...]


class ConstraintGraph:
    def __init__(self) -> None:
        self._names: list[str] = []
        self._index: dict[str, int] = {}
        # 邻接表：u -> [(v, w, tag)]
        self._adj: list[list[PredEdge]] = []
        self._radj: list[list[PredEdge]] = []

    # ---- 变量管理 ----
    def add_variable(self, name: str) -> int:
        if name in self._index:
            return self._index[name]
        idx = len(self._names)
        self._names.append(name)
        self._index[name] = idx
        self._adj.append([])
        self._radj.append([])
        return idx

    def index_of(self, name: str) -> int:
        return self._index[name]

    def has_variable(self, name: str) -> bool:
        return name in self._index

    @property
    def variable_count(self) -> int:
        return len(self._names)

    def name_of(self, idx: int) -> str:
        return self._names[idx]

    # ---- 约束添加 ----
    def add_inequality(self, u: int, v: int, w: float, tag: Tag) -> None:
        """添加约束 x_v - x_u <= w（边 u -> v，权重 w）。"""
        edge = (v, w, tag)
        redge = (u, w, tag)
        self._adj[u].append(edge)
        self._radj[v].append(redge)

    def add_le(self, x: int, y: int, w: float, tag: Tag) -> None:
        """约束 x - y <= w。边 y -> x，权重 w。"""
        self.add_inequality(y, x, w, tag)

    def add_ge(self, x: int, y: int, w: float, tag: Tag) -> None:
        """约束 x - y >= w，等价于 y - x <= -w。边 x -> y，权重 -w。"""
        self.add_inequality(x, y, -w, tag)

    def add_eq_diff(self, x: int, y: int, diff: float, tol: float, tag: Tag) -> None:
        """约束 |(x - y) - diff| <= tol，拆成两条不等式。"""
        self.add_le(x, y, diff + tol, tag)
        self.add_ge(x, y, diff - tol, tag)

    # ---- Bellman-Ford ----
    @staticmethod
    def _relax(
        adj: list[list[PredEdge]],
        dist: list[float],
        pred: list[PredEdge | None],
        eps: float = 1e-12,
    ) -> bool:
        changed = False
        for u, du in enumerate(dist):
            if du == math.inf:
                continue
            for v, w, tag in adj[u]:
                cand = du + w
                if cand < dist[v] - eps:
                    dist[v] = cand
                    pred[v] = (u, w, tag)
                    changed = True
        return changed

    def _super_source_cycle(self) -> NegativeCycle | None:
        """全零初始化（等价超级源）跑 n 轮，检测任意负环并回溯。"""
        n = self.variable_count
        dist = [0.0] * n
        pred: list[PredEdge | None] = [None] * n
        still = False
        for _ in range(n):
            still = self._relax(self._adj, dist, pred)
            if not still:
                break
        if not still:
            return None
        # 找一个第 n 轮仍被松弛的节点作为回溯起点
        start = self._last_relaxed_node()
        return self._extract_cycle(start, pred)

    def _last_relaxed_node(self) -> int:
        n = self.variable_count
        dist = [0.0] * n
        pred: list[PredEdge | None] = [None] * n
        last = 0
        for _ in range(n):
            for u, du in enumerate(dist):
                for v, w, tag in self._adj[u]:
                    cand = du + w
                    if cand < dist[v] - 1e-12:
                        dist[v] = cand
                        pred[v] = (u, w, tag)
                        last = v
        return last

    @staticmethod
    def _extract_cycle(
        start: int, pred: list[PredEdge | None]
    ) -> NegativeCycle | None:
        seen: dict[int, int] = {}
        path_nodes: list[int] = []
        node: int | None = start
        while node is not None and node not in seen:
            seen[node] = len(path_nodes)
            path_nodes.append(node)
            pe = pred[node]
            node = pe[0] if pe is not None else None
        if node is None:  # pragma: no cover - 被松弛节点必有前驱
            return None
        cyc = path_nodes[seen[node]:]
        edges: list[PredEdge] = []
        for v in cyc:
            pe = pred[v]
            if pe is None:  # pragma: no cover - 环上节点必有前驱边
                return None
            edges.append(pe)
        return NegativeCycle(tuple(edges))

    def _shortest_from(
        self, adj: list[list[PredEdge]], source: int
    ) -> list[float]:
        """单源最短路；source 距离为 0，其余 +inf，不可达即无界。"""
        n = self.variable_count
        dist = [math.inf] * n
        dist[source] = 0.0
        pred: list[PredEdge | None] = [None] * n
        for _ in range(n - 1):
            if not self._relax(adj, dist, pred):
                break
        return dist

    def find_negative_cycle(self, ref: int) -> NegativeCycle | None:
        # ref 参数保留接口语义；负环检测依赖超级源，与 REF 选择无关。
        del ref
        return self._super_source_cycle()

    def solve_bounds(self, ref: int) -> dict[int, tuple[float, float]]:
        """返回每个变量相对 REF 的可行区间 (下界, 上界)。

        上界：原图最短路（x - REF <= d）。
        下界：反向图最短路 d' 给出 REF - x <= d'，即 x >= -d'。
        调用前应先用 find_negative_cycle 确认可行。
        """
        upper = self._shortest_from(self._adj, ref)
        rdist = self._shortest_from(self._radj, ref)
        bounds: dict[int, tuple[float, float]] = {}
        for i in range(self.variable_count):
            lower = -rdist[i] if rdist[i] != math.inf else -math.inf
            bounds[i] = (lower, upper[i])
        return bounds
