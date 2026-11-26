"""Small deterministic graph algorithms used by the planner."""

from __future__ import annotations

import sys
from typing import Hashable, Iterable

Node = Hashable


def build_adjacency(nodes: Iterable[Node], edges: Iterable[tuple[Node, Node]]):
    node_set = list(dict.fromkeys(nodes))
    adj: dict[Node, set[Node]] = {node: set() for node in node_set}
    for source, target in edges:
        adj.setdefault(source, set()).add(target)
        adj.setdefault(target, set())
    return node_set, adj


def topological_sort(
    nodes: Iterable[Node], edges: Iterable[tuple[Node, Node]]
) -> tuple[list[Node], list[tuple[Node, ...]]]:
    """Return a dependency-first order and zero or more cycles.

    An edge ``(dependency, caller)`` means the dependency must be started
    before the caller.  When a cycle exists, the returned order contains the
    acyclic prefix and cycles are returned as node paths whose first node is
    repeated as the last element.
    """

    node_list, adj = build_adjacency(nodes, edges)
    indegree = {node: 0 for node in adj}
    for source in adj:
        for target in adj[source]:
            indegree[target] = indegree.get(target, 0) + 1

    ready = sorted(node for node, degree in indegree.items() if degree == 0)
    order: list[Node] = []
    while ready:
        node = ready.pop(0)
        order.append(node)
        for target in sorted(adj.get(node, ())):
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
        ready.sort()

    if len(order) == len(adj):
        return order, []

    remaining = [node for node in adj if indegree[node] > 0]
    cycles = find_cycles(remaining, [(u, v) for u in adj for v in adj[u]])
    return order, cycles


def find_cycles(
    nodes: Iterable[Node], edges: Iterable[tuple[Node, Node]]
) -> list[tuple[Node, ...]]:
    _, adj = build_adjacency(nodes, edges)
    sccs = _strongly_connected_components(adj)
    cycles: list[tuple[Node, ...]] = []
    for component in sccs:
        component_set = set(component)
        start = min(component_set, key=str)
        internal_adj = {
            node: sorted(neighbor for neighbor in adj.get(node, ()) if neighbor in component_set)
            for node in component_set
        }
        is_self_loop = start in internal_adj.get(start, [])
        if len(component_set) == 1 and not is_self_loop:
            continue
        cycle = _extract_cycle(start, internal_adj)
        if cycle:
            cycles.append(tuple(cycle))
    cycles.sort(key=lambda cycle: tuple(str(item) for item in cycle))
    return cycles


def _strongly_connected_components(adj: dict[Node, set[Node]]) -> list[list[Node]]:
    """Tarjan SCC with a raised recursion limit; planning graphs are small."""

    old_limit = sys.getrecursionlimit()
    sys.setrecursionlimit(max(old_limit, len(adj) * 4 + 100))
    index = 0
    indices: dict[Node, int] = {}
    lowlink: dict[Node, int] = {}
    stack: list[Node] = []
    on_stack: set[Node] = set()
    result: list[list[Node]] = []

    def strong_connect(node: Node) -> None:
        nonlocal index
        indices[node] = index
        lowlink[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)

        for neighbor in sorted(adj.get(node, ()), key=str):
            if neighbor not in indices:
                strong_connect(neighbor)
                lowlink[node] = min(lowlink[node], lowlink[neighbor])
            elif neighbor in on_stack:
                lowlink[node] = min(lowlink[node], indices[neighbor])

        if lowlink[node] == indices[node]:
            component: list[Node] = []
            while True:
                member = stack.pop()
                on_stack.remove(member)
                component.append(member)
                if member == node:
                    break
            result.append(component)

    try:
        for node in sorted(adj, key=str):
            if node not in indices:
                strong_connect(node)
    finally:
        sys.setrecursionlimit(old_limit)
    return result


def _extract_cycle(
    start: Node, adj: dict[Node, list[Node]]
) -> list[Node] | None:
    path = [start]
    on_path = {start}

    def visit(node: Node) -> list[Node] | None:
        for neighbor in adj.get(node, []):
            if neighbor == start:
                return path + [start]
            if neighbor in on_path:
                continue
            path.append(neighbor)
            on_path.add(neighbor)
            found = visit(neighbor)
            if found is not None:
                return found
            path.pop()
            on_path.remove(neighbor)
        return None

    return visit(start)
