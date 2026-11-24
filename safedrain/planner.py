"""缩容规划搜索。

状态空间是“稳定态 placement -> 稳定态 placement”的迁移：每条边是一个完整的
两阶段 :class:`~safedrain.model.Action`（同机闭包整体搬迁）。采用逐层 BFS，
每个深度的动作按固定字典序展开，因此第一条到达目标的路径同时满足：

1. 搬迁次数最少（BFS 最短路径）；
2. 动作序最小（同层字典序展开 + 首次到达即记录）。

退役节点数由调用方指定的退役集合决定，:func:`plan_max_retirement` 在其外层
按“退役节点最多、再比搬迁次数、再比动作序”枚举退役子集。
"""

from __future__ import annotations

from collections import deque

from .model import (
    Action,
    ExplorationLimit,
    PairType,
    Plan,
    PlanningError,
    Topology,
)
from .simulate import Snapshot, initial_snapshot, simulate_action


# ---------------------------------------------------------------------------
# 同机闭包
# ---------------------------------------------------------------------------

def same_node_cliques(topology: Topology) -> list[tuple[str, ...]]:
    """按 SAME_NODE 约束做并查集，返回每个闭包内按 Pod id 排序的元组。"""
    parent = {p.id: p.id for p in topology.pods}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for pair in topology.pairs:
        if pair.kind is PairType.SAME_NODE:
            union(pair.a, pair.b)

    groups: dict[str, list[str]] = {}
    for pod_id in sorted(parent):
        groups.setdefault(find(pod_id), []).append(pod_id)
    return [tuple(members) for members in groups.values()]


# ---------------------------------------------------------------------------
# BFS
# ---------------------------------------------------------------------------

def _goal(placement: tuple[str, ...], retiring: frozenset[str]) -> bool:
    return all(node_id not in retiring for node_id in placement)


def _expand(topology: Topology, placement: tuple[str, ...],
            cliques: list[tuple[str, ...]]) -> list[Action]:
    """枚举当前稳定态下所有合法的单步动作（已通过两阶段模拟验证）。"""
    pod_index = {p.id: i for i, p in enumerate(topology.pods)}
    actions: list[Action] = []
    for clique in cliques:
        head = topology.pod_map[clique[0]]
        source = placement[pod_index[clique[0]]]
        # 闭包内所有成员必须在同一节点（稳定态已保证），且都不可标记不可移动。
        if any(placement[pod_index[pid]] != source for pid in clique):
            continue
        if any(topology.pod_map[pid].immovable for pid in clique):
            continue
        for dest in topology.destinations_for(head):
            if dest == source:
                continue
            action = Action(source, dest, clique)
            snapshot = Snapshot("stable", placement, ())
            try:
                simulate_action(topology, snapshot, action)
            except PlanningError:
                continue
            actions.append(action)
    # 固定字典序：先目的地、再源、再 clique 内容，保证动作序可比。
    actions.sort(key=lambda a: (a.destination, a.source, a.clique))
    return actions


def _bfs(topology: Topology, retiring: frozenset[str], *, max_states: int,
         max_actions: int, keep_paths: bool):
    snapshot = initial_snapshot(topology)
    start = snapshot.placement
    if _goal(start, retiring):
        return Plan(retiring, (), 0) if keep_paths else True

    cliques = same_node_cliques(topology)
    # 队列元素：(当前 placement, 父状态, 触发动作)。
    queue: deque[tuple] = deque([(start, None, None)])
    parents: dict[tuple[str, ...], tuple] = {start: (None, None)}
    visited: set[tuple[str, ...]] = {start}
    explored_states = 0
    tried_actions = 0
    goal_state: tuple[str, ...] | None = None

    while queue:
        placement, _, _ = queue.popleft()
        explored_states += 1
        if explored_states > max_states:
            raise ExplorationLimit(f"状态数超过预算 {max_states}")
        for action in _expand(topology, placement, cliques):
            tried_actions += 1
            if tried_actions > max_actions:
                raise ExplorationLimit(f"动作尝试数超过预算 {max_actions}")
            events = simulate_action(
                topology, Snapshot("stable", placement, ()), action
            )
            nxt = events[-1].snapshot.placement
            if nxt in visited:
                continue
            visited.add(nxt)
            parents[nxt] = (placement, action)
            if _goal(nxt, retiring):
                goal_state = nxt
                break
            queue.append((nxt, placement, action))
        if goal_state is not None:
            break

    if goal_state is None:
        return None
    if not keep_paths:
        return True

    # 沿父指针回溯，再反转为正向动作序列。
    reversed_actions: list[Action] = []
    state = goal_state
    while parents[state][0] is not None:
        parent, action = parents[state]
        reversed_actions.append(action)
        state = parent
    reversed_actions.reverse()
    actions = tuple(
        Action(a.source, a.destination, a.clique, f"T{i + 1}")
        for i, a in enumerate(reversed_actions)
    )
    return Plan(retiring, actions, len(actions))


def plan_for_retirement(
    topology: Topology,
    retiring: set[str] | frozenset[str],
    *,
    max_states: int = 200_000,
    max_actions: int = 1_000_000,
) -> Plan | None:
    """为指定退役节点集合找搬迁次数最少、动作序最小的可行计划。

    不可行时返回 None。输入的退役集合必须是拓扑中存在的节点。
    """
    retiring = frozenset(retiring)
    unknown = retiring - {n.id for n in topology.nodes}
    if unknown:
        raise PlanningError(f"退役集合包含未知节点 {sorted(unknown)}")
    try:
        return _bfs(topology, retiring, max_states=max_states,
                    max_actions=max_actions, keep_paths=True)
    except ExplorationLimit:
        raise


def is_feasible(topology: Topology, retiring: frozenset[str], *,
                max_states: int = 200_000, max_actions: int = 1_000_000) -> bool:
    """只判断可行性，不保留路径，预算耗尽时按 ExplorationLimit 抛出。"""
    result = _bfs(topology, retiring, max_states=max_states,
                  max_actions=max_actions, keep_paths=False)
    return bool(result)


# ---------------------------------------------------------------------------
# 最大退役集合
# ---------------------------------------------------------------------------

def plan_max_retirement(
    topology: Topology,
    candidates: set[str] | frozenset[str] | None = None,
    *,
    max_states: int = 200_000,
    max_actions: int = 1_000_000,
) -> Plan:
    """在候选节点中找退役最多的方案，同规模比搬迁次数，再比动作序。

    退役子集按“规模降序、id 字典序升序”枚举；同规模下第一个可行子集的
    BFS 最优计划即全局最优，因为 BFS 已保证该子集内搬迁次数/动作序最优。
    若没有任何节点可退（含空集场景），返回 0 动作的空计划。
    """
    node_ids = {n.id for n in topology.nodes}
    if candidates is None:
        candidates = node_ids
    candidates = frozenset(candidates)
    unknown = candidates - node_ids
    if unknown:
        raise PlanningError(f"候选集合包含未知节点 {sorted(unknown)}")

    ordered = sorted(candidates)
    n = len(ordered)
    # 空集始终可行（目标天然满足），作为兜底。
    best: Plan = Plan(frozenset(), (), 0)
    for size in range(n, 0, -1):
        found_at_size: Plan | None = None
        for combo in _combinations(ordered, size):
            retiring = frozenset(combo)
            plan = plan_for_retirement(
                topology, retiring,
                max_states=max_states, max_actions=max_actions,
            )
            if plan is not None:
                found_at_size = plan
                break
        if found_at_size is not None:
            return found_at_size
    return best


def _combinations(items: list[str], size: int):
    """按字典序产出固定大小组合（避免依赖 itertools 的顺序假设）。"""
    from itertools import combinations

    yield from combinations(items, size)
