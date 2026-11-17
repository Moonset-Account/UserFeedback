"""启动图与可达性推演：边门控、BFS 展开、(节点, 状态) 去重。

关键正确性约束：
  * 每条边可用 requires_effective 声明执行所需 effective 能力，不满足则不可达；
  * 展开键为 (目标节点, 新 CapState)，同一状态再次到达即终止，循环天然收敛；
  * 不同分支到达同一节点时按各自状态分别保存，绝不做跨分支能力并集；
  * BFS 首次到达即最短操作序列，越界状态据此给出最短路径。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any

from .caps import to_mask, to_names
from .state import CapState
from .transitions import TransitionError, apply_operation


@dataclass(frozen=True)
class Edge:
    """一条启动图边。

    op 为操作字典（见 transitions.apply_operation）；
    requires_effective 声明通过该边所需的 effective 能力名（全部满足才可达）。
    """

    src: str
    dst: str
    op: dict[str, Any]
    requires_effective: tuple[str, ...] = ()
    label: str | None = None

    def edge_id(self) -> str:
        return self.label or f"{self.src}->{self.dst}"

    def require_mask(self) -> int:
        return to_mask(self.requires_effective)


class Graph:
    def __init__(
        self,
        edges: list[Edge | dict[str, Any]],
        exits: list[str] | None = None,
    ) -> None:
        self.edges: list[Edge] = []
        self.adj: dict[str, list[Edge]] = {}
        for raw in edges:
            edge = raw if isinstance(raw, Edge) else Edge(
                src=raw["src"],
                dst=raw["dst"],
                op=raw["op"],
                requires_effective=tuple(raw.get("requires_effective", ())),
                label=raw.get("label"),
            )
            self.edges.append(edge)
            self.adj.setdefault(edge.src, []).append(edge)
        self.declared_exits = list(exits or [])
        self.nodes = self._collect_nodes()

    def _collect_nodes(self) -> set[str]:
        nodes: set[str] = set()
        for edge in self.edges:
            nodes.add(edge.src)
            nodes.add(edge.dst)
        return nodes

    def exit_nodes(self) -> set[str]:
        """显式出口 ∪ 无出边的叶子节点。"""
        leaves = {node for node in self.nodes if node not in self.adj}
        return set(self.declared_exits) | leaves

    def outgoing(self, node: str) -> list[Edge]:
        return self.adj.get(node, [])


@dataclass(frozen=True)
class Reached:
    node: str
    state: CapState
    path: tuple[dict[str, Any], ...]
    depth: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "node": self.node,
            "depth": self.depth,
            "state": self.state.to_dict(),
            "path": list(self.path),
        }


def _path_step(edge: Edge) -> dict[str, Any]:
    return {"edge": edge.edge_id(), "src": edge.src, "dst": edge.dst, "op": edge.op}


def analyze(
    graph: Graph,
    initial: CapState,
    allowed: Any = None,
    start: str | None = None,
) -> dict[str, Any]:
    """对启动图做完整可达性推演。

    allowed 为允许集合（能力名/掩码）；状态的 live 集合（P∪E∪A）中出现
    allowed 之外的能力即判越界，并给出到达该状态的最短操作序列。
    返回结构全部 JSON 友好（能力以名称表示）。
    """
    if start is None:
        if not graph.nodes:
            raise ValueError("空图且未指定 start 节点")
        # 确定性：取声明/叶子之外、且有出边的最小节点；否则最小节点
        starters = sorted(n for n in graph.nodes if graph.outgoing(n))
        start = starters[0] if starters else sorted(graph.nodes)[0]

    allowed_mask = to_mask(allowed) if allowed is not None else None

    start_reached = Reached(node=start, state=initial, path=(), depth=0)
    queue: deque[Reached] = deque([start_reached])
    # (node, state) -> 首次（最短）Reached
    visited: dict[tuple[str, CapState], Reached] = {(start, initial): start_reached}
    blocked: list[dict[str, Any]] = []

    while queue:
        cur = queue.popleft()
        for edge in graph.outgoing(cur.node):
            need = edge.require_mask()
            if cur.state.e & need != need:
                blocked.append({
                    "at_node": cur.node,
                    "edge": edge.edge_id(),
                    "reason": "effective 门控不满足",
                    "requires": list(edge.requires_effective),
                    "effective": to_names(cur.state.e),
                })
                continue
            try:
                new_state = apply_operation(cur.state, edge.op)
            except TransitionError as exc:
                blocked.append({
                    "at_node": cur.node,
                    "edge": edge.edge_id(),
                    "reason": f"转换前提不满足: {exc}",
                    "op": edge.op,
                })
                continue
            key = (edge.dst, new_state)
            if key in visited:
                # 同样的状态再次到达：终止该分支展开
                continue
            nxt = Reached(
                node=edge.dst,
                state=new_state,
                path=cur.path + (_path_step(edge),),
                depth=cur.depth + 1,
            )
            visited[key] = nxt
            queue.append(nxt)

    exit_set = graph.exit_nodes()
    # 仅实际到达过的节点才能成为出口：转换失败/门控阻断的边指向的节点不可达，
    # 不得计入出口状态。
    reachable_nodes = {node for (node, _state) in visited.keys()}
    exit_set &= reachable_nodes
    exit_states: dict[str, list[dict[str, Any]]] = {}
    for (node, _state), reached in sorted(visited.items(), key=lambda kv: (kv[1].depth, kv[0][0])):
        if node in exit_set:
            exit_states.setdefault(node, []).append(reached.to_dict())

    violations: list[dict[str, Any]] = []
    if allowed_mask is not None:
        for reached in visited.values():
            extra = reached.state.live_mask() & ~allowed_mask
            if extra:
                violations.append({
                    "node": reached.node,
                    "depth": reached.depth,
                    "extra_caps": to_names(extra),
                    "state": reached.state.to_dict(),
                    "shortest_sequence": list(reached.path),
                })
        violations.sort(key=lambda item: (item["depth"], item["node"]))

    all_reached = [r.to_dict() for r in sorted(visited.values(), key=lambda r: (r.depth, r.node))]
    return {
        "start": start,
        "initial_state": initial.to_dict(),
        "allowed": to_names(allowed_mask) if allowed_mask is not None else None,
        "reached_count": len(visited),
        "exit_nodes": sorted(exit_set),
        "exit_states": exit_states,
        "blocked_edges": blocked,
        "violations": violations,
        "all_reached": all_reached,
    }
