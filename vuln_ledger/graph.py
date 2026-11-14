"""有向提交图遍历：可达子图、拓扑序、环检测。

图的边方向为 子提交 -> 父提交（commits[*].parents）。
维护线根提交是最老的历史起点（自身没有父提交），从根出发需要沿
“子 -> 父”边的反向（即 children 方向）向下游走，才能覆盖整条维护线。
"""
from __future__ import annotations

from collections import deque

from .models import Commit, InputError


def _build_children(commits: dict[str, Commit]) -> dict[str, list[str]]:
    children: dict[str, list[str]] = {cid: [] for cid in commits}
    for cid, commit in commits.items():
        for parent in commit.parents:
            children[parent].append(cid)
    return children


def reachable_order(
    commits: dict[str, Commit], roots: list[str]
) -> tuple[list[str], set[str], dict[str, list[str]]]:
    """从维护线根提交出发沿 children 方向 BFS。

    返回 (BFS 序, 可达节点集合, 全局 children 索引)。
    """
    children = _build_children(commits)
    seen: set[str] = set()
    order: list[str] = []
    queue: deque[str] = deque()
    for root in roots:
        if root not in seen:
            seen.add(root)
            queue.append(root)
    while queue:
        cid = queue.popleft()
        order.append(cid)
        for child in children[cid]:
            if child not in seen:
                seen.add(child)
                queue.append(child)
    return order, seen, children


def topological_order(
    commits: dict[str, Commit], node_set: set[str]
) -> list[str]:
    """在可达子图上做 Kahn 拓扑排序（父先于子），检测有向环。

    边为 cid -> parent(cid)；父先于子意味着节点在其所有子节点之前输出，
    因此以“子节点数（出向 parents 数）”作为 Kahn 的待清零计数。
    """
    remaining_parents: dict[str, int] = {
        cid: sum(1 for p in commits[cid].parents if p in node_set)
        for cid in node_set
    }
    children = _build_children(commits)

    ready = deque(sorted(cid for cid, deg in remaining_parents.items() if deg == 0))
    order: list[str] = []
    while ready:
        cid = ready.popleft()
        order.append(cid)
        for child in children[cid]:
            if child in remaining_parents:
                remaining_parents[child] -= 1
                if remaining_parents[child] == 0:
                    ready.append(child)

    if len(order) != len(node_set):
        stuck = sorted(cid for cid, deg in remaining_parents.items() if deg > 0)
        raise InputError(f"提交图存在有向环，涉及提交: {', '.join(stuck)}")
    return order


def assert_acyclic(commits: dict[str, Commit]) -> None:
    """全图环检测：即使环不被任何维护线根覆盖也要拒绝。"""
    topological_order(commits, set(commits.keys()))


def ancestors_bfs(commits: dict[str, Commit], source: str) -> list[str]:
    """从来源提交沿 parents 向上 BFS，返回含来源提交本身的祖先遍历序。"""
    seen = {source}
    order: list[str] = []
    queue: deque[str] = deque([source])
    while queue:
        cid = queue.popleft()
        order.append(cid)
        for parent in commits[cid].parents:
            if parent not in seen:
                seen.add(parent)
                queue.append(parent)
    return order
