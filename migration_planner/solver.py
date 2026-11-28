"""最小代价反馈边集（minimum-cost feedback edge set）求解。

暂缓一条可暂缓的外键边等价于把它从依赖图中删掉；目标是用最小的总代价
删掉一组边，使剩余图无环（无环即可拓扑排序后顺序插入）。

该问题本质上是 NP 难的，本模块提供：

* :func:`minimum_feedback_edge_set`：带剪枝的分支定界精确算法；
* :func:`brute_force_minimum_feedback_edge_set`：2^n 穷举，题目保证可暂缓
  外键组在十二组以内时，可用它与分支定界结果逐一核对。

若不可暂缓边自身成环，则无解，两个函数都返回 ``None``。
"""

from __future__ import annotations

from .graph import DepEdge, Node, find_unbreakable_cycle


def _kept_graph_acyclic(edges: list[DepEdge], kept_ids: set[int]) -> bool:
    adjacency: dict[Node, list[Node]] = {}
    for e in edges:
        if e.edge_id in kept_ids:
            adjacency.setdefault(e.source, []).append(e.target)
            adjacency.setdefault(e.target, [])
    white, gray, black = 0, 1, 2
    color = {n: white for n in adjacency}

    def dfs(u: Node) -> bool:
        color[u] = gray
        for v in adjacency[u]:
            if color[v] == gray:
                return False
            if color[v] == white and not dfs(v):
                return False
        color[u] = black
        return True

    return all(color[n] != white or dfs(n) for n in adjacency)


def _creates_cycle(
    adjacency: dict[Node, list[Node]], edge: DepEdge
) -> bool:
    """在已无环的 adjacency 上加入 edge（source->target）是否成环。

    等价于判断从 target 能否沿现有边到达 source。
    """
    stack = [edge.target]
    seen = {edge.target}
    while stack:
        u = stack.pop()
        if u == edge.source:
            return True
        for v in adjacency.get(u, ()):
            if v not in seen:
                seen.add(v)
                stack.append(v)
    return False


def minimum_feedback_edge_set(
    nodes: list[Node], edges: list[DepEdge]
) -> tuple[frozenset[int], float] | None:
    """分支定界求最小代价暂缓边集。

    返回 ``(暂缓边 id 集合, 总代价)``；不可暂缓边自身成环时返回 ``None``。
    代价相同时，选择边 id 元组字典序最小的方案，保证结果确定。
    """
    if find_unbreakable_cycle(nodes, edges) is not None:
        return None

    breakable = [e for e in edges if e.breakable]
    unbreakable = [e for e in edges if not e.breakable]

    adjacency: dict[Node, list[Node]] = {n: [] for n in nodes}
    for e in unbreakable:
        adjacency[e.source].append(e.target)

    best: tuple[float, tuple[int, ...], frozenset[int]] | None = None
    removed: set[int] = set()
    current_cost = 0.0

    def consider(candidate: frozenset[int], cost: float) -> None:
        nonlocal best
        key = (cost, tuple(sorted(candidate)), candidate)
        if best is None or key[:2] < best[:2]:
            best = (cost, tuple(sorted(candidate)), candidate)

    def dfs(i: int) -> None:
        nonlocal current_cost
        if best is not None and current_cost > best[0]:
            return  # 当前代价已超过已知最优，剪掉
        if i == len(breakable):
            consider(frozenset(removed), current_cost)
            return
        edge = breakable[i]

        # 分支 1：保留该边（优先，倾向先找到低代价方案）。
        if not _creates_cycle(adjacency, edge):
            adjacency[edge.source].append(edge.target)
            dfs(i + 1)
            adjacency[edge.source].pop()

        # 分支 2：暂缓（删除）该边。
        removed.add(edge.edge_id)
        current_cost += edge.cost
        dfs(i + 1)
        current_cost -= edge.cost
        removed.remove(edge.edge_id)

    dfs(0)
    if best is None:
        return None
    return best[2], best[0]


def brute_force_minimum_feedback_edge_set(
    nodes: list[Node], edges: list[DepEdge]
) -> tuple[frozenset[int], float] | None:
    """穷举所有可暂缓边的子集，返回最小代价方案，供测试交叉核对。

    可暂缓边超过 12 组时拒绝执行（2^n 过大，也超出题目约定的核对范围）。
    """
    if find_unbreakable_cycle(nodes, edges) is not None:
        return None

    breakable = [e for e in edges if e.breakable]
    if len(breakable) > 12:
        raise ValueError("穷举核对仅支持不超过 12 组可暂缓外键")

    breakable_ids = [e.edge_id for e in breakable]
    cost_of = {e.edge_id: e.cost for e in breakable}
    best: tuple[float, tuple[int, ...], frozenset[int]] | None = None

    for mask in range(1 << len(breakable)):
        removed = {
            eid for bit, eid in enumerate(breakable_ids) if mask & (1 << bit)
        }
        kept = {e.edge_id for e in edges if e.edge_id not in removed}
        if not _kept_graph_acyclic(edges, kept):
            continue
        cost = sum(cost_of[eid] for eid in removed)
        key = (cost, tuple(sorted(removed)), frozenset(removed))
        if best is None or key[:2] < best[:2]:
            best = key  # type: ignore[assignment]

    if best is None:
        return None
    return best[2], best[0]
