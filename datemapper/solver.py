"""差分约束求解器：整数偏移、最小最大改动、字典序最小解与矛盾证书。

模型
----
每个人 p 的所有事件整体平移整数天，偏移量记为整数变量 ``x_p``。

所有输入约束都能写成差分约束 ``x_v - x_u <= w``，它等价于图上一条
u -> v、权重为 w 的边。以一个恒等于 0 的锚点节点 ``ZERO`` 表示常量：

* 偏移上界 ``x_p <= hi_p``  : ZERO -> p，权重 hi_p
* 偏移下界 ``x_p >= lo_p``  : p -> ZERO，权重 -lo_p
* 事件间隔下界
  ``(d_b + x_pb) - (d_a + x_pa) >= min_gap``：pb -> pa，权重 d_a - d_b + min_gap
* 事件间隔上界（同形式）  ：pa -> pb，权重 d_b - d_a - min_gap，
  对 max_gap 即取 min_gap=max_gap 的另一条边。

系统可行当且仅当图中不存在负权环。检测到负环时，环上的不等式逐条相加会
坍缩成 ``0 <= 负数``，构成可独立复核的矛盾证书。

目标按题面分两级：
1. 最小化 ``max_p |x_p - proposed_p|``（任何人的最大偏移改动）；
2. 在所有一级最优解中，按人号顺序取字典序最小的偏移向量。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

ZERO = 0  # 锚点节点；人的变量节点编号为 1..n


@dataclass(frozen=True)
class Edge:
    """一条差分约束 x_v - x_u <= weight，可选地记录其来源。"""

    u: int
    v: int
    weight: int
    origin: object = None


def _negative_cycle(
    edges: Sequence[Edge], node_count: int
) -> Optional[List[Edge]]:
    """Bellman-Ford 检测负权环，存在时返回构成环的边列表（按环序）。

    全部节点初始距离取 0，等价于增设一个连向所有节点、权重为 0 的超级源，
    因此不经过锚点 ZERO 的负环（例如自身矛盾的单人约束）同样可被发现。
    """

    dist = [0] * node_count
    predecessor: List[Optional[Edge]] = [None] * node_count
    updated_node: Optional[int] = None

    for _ in range(node_count):
        updated_node = None
        for edge in edges:
            candidate = dist[edge.u] + edge.weight
            if candidate < dist[edge.v]:
                dist[edge.v] = candidate
                predecessor[edge.v] = edge
                updated_node = edge.v
        if updated_node is None:
            return None

    # 第 node_count 轮仍有松弛：updated_node 要么在负环上，要么可沿
    # 前驱链到达负环。沿链走 node_count 步后必然落在某个环节点上。
    node = updated_node
    assert node is not None
    for _ in range(node_count):
        prev_edge = predecessor[node]
        assert prev_edge is not None
        node = prev_edge.u

    cycle_node = node
    cycle: List[Edge] = []
    current = cycle_node
    while True:
        edge = predecessor[current]
        assert edge is not None
        cycle.append(edge)
        current = edge.u
        if current == cycle_node:
            break
    cycle.reverse()
    return cycle


def _shortest_distances(
    edges: Sequence[Edge], node_count: int, reverse: bool = False
) -> Optional[List[int]]:
    """从锚点 ZERO 出发的最短路；reverse=True 时在反图上计算。

    反图上从 ZERO 得到的距离 dist_rev[i]，等于原图中 i -> ZERO 的最短路。
    图含负环（系统不可行）时返回 None。
    """

    dist: List[Optional[int]] = [None] * node_count
    dist[ZERO] = 0
    for _ in range(node_count - 1):
        changed = False
        for edge in edges:
            if reverse:
                u, v = edge.v, edge.u
            else:
                u, v = edge.u, edge.v
            if dist[u] is not None:
                candidate = dist[u] + edge.weight
                if dist[v] is None or candidate < dist[v]:
                    dist[v] = candidate
                    changed = True
        if not changed:
            break
    # 再松弛一轮确认没有负环
    for edge in edges:
        if reverse:
            u, v = edge.v, edge.u
        else:
            u, v = edge.u, edge.v
        if dist[u] is not None and (
            dist[v] is None or dist[u] + edge.weight < dist[v]
        ):
            return None
    return [0 if d is None else d for d in dist]


@dataclass(frozen=True)
class Inequality:
    """矛盾证书中的一条不等式：x_left - x_right <= right_hand_side。

    变量名是人的标识；常量锚点（偏移上下界）使用变量名 None。
    """

    left: object
    right: object
    right_hand_side: int
    origin: object = None

    def to_text(self) -> str:
        left = "0" if self.left is None else f"x[{self.left!r}]"
        right = "0" if self.right is None else f"x[{self.right!r}]"
        return f"{left} - {right} <= {self.right_hand_side}"


@dataclass
class ContradictionCertificate:
    """无解时的矛盾证书：一组可独立复核的不等式。

    逐条相加后所有变量项互相抵消，得到 ``0 <= contradiction_sum``，
    而 contradiction_sum 为负数，因此系统无解。
    """

    inequalities: List[Inequality]
    contradiction_sum: int

    def verify(self) -> bool:
        """独立复核：变量是否完全抵消、右端项之和是否为负。"""

        balance: Dict[object, int] = {}
        total = 0
        for item in self.inequalities:
            if item.left is not None:
                balance[item.left] = balance.get(item.left, 0) + 1
            if item.right is not None:
                balance[item.right] = balance.get(item.right, 0) - 1
            total += item.right_hand_side
        return all(value == 0 for value in balance.values()) and total < 0 and total == self.contradiction_sum

    def to_text(self) -> str:
        lines = [item.to_text() for item in self.inequalities]
        lines.append(f"相加得 0 <= {self.contradiction_sum}，矛盾。")
        return "\n".join(lines)


class InfeasibleError(Exception):
    """差分约束系统无解，携带可独立验证的矛盾证书。"""

    def __init__(self, certificate: ContradictionCertificate):
        self.certificate = certificate
        super().__init__("无可行偏移；矛盾证书:\n" + certificate.to_text())


def _build_certificate(cycle: Sequence[Edge], node_to_person: Dict[int, object]):
    inequalities = [
        Inequality(
            left=node_to_person.get(edge.v),
            right=node_to_person.get(edge.u),
            right_hand_side=edge.weight,
            origin=edge.origin,
        )
        for edge in cycle
    ]
    total = sum(item.right_hand_side for item in inequalities)
    return ContradictionCertificate(inequalities=inequalities, contradiction_sum=total)


@dataclass(frozen=True)
class SolveResult:
    """求解结果。

    offsets：人号 -> 定稿偏移（整数天）；
    max_change：max_p |offsets[p] - proposed[p]|。
    """

    offsets: Dict[object, int]
    max_change: int


def solve_offsets(
    persons: Sequence[object],
    lower_bounds: Dict[object, int],
    upper_bounds: Dict[object, int],
    proposed: Dict[object, int],
    base_edges: Sequence[Edge],
) -> SolveResult:
    """求两级最优的整数偏移向量；无解时抛 InfeasibleError。

    base_edges 已包含偏移上下界与全部事件间隔约束。
    """

    ordered = list(persons)
    n = len(ordered)
    if n == 0:
        return SolveResult(offsets={}, max_change=0)
    node_count = n + 1
    person_to_node = {person: index + 1 for index, person in enumerate(ordered)}
    node_to_person = {index + 1: person for index, person in enumerate(ordered)}

    base_edges = list(base_edges)

    cycle = _negative_cycle(base_edges, node_count)
    if cycle is not None:
        raise InfeasibleError(_build_certificate(cycle, node_to_person))

    def feasible(extra: Sequence[Edge]) -> Optional[List[Edge]]:
        if not extra:
            return None
        return _negative_cycle(base_edges + list(extra), node_count)

    # ---- 第一级：二分最小化 D = max_p |x_p - proposed_p| ----
    def deviation_edges(d_value: int) -> List[Edge]:
        edges: List[Edge] = []
        for person in ordered:
            node = person_to_node[person]
            origin = ("deviation", person)
            edges.append(Edge(ZERO, node, proposed[person] + d_value, origin))
            edges.append(Edge(node, ZERO, d_value - proposed[person], origin))
        return edges

    # D 的上界：偏移区间内离 proposed 最远的点，必然可行（基础系统已可行）。
    hi_d = max(
        max(abs(lower_bounds[p] - proposed[p]), abs(upper_bounds[p] - proposed[p]))
        for p in ordered
    )
    lo_d = 0
    best_d = hi_d
    while lo_d <= hi_d:
        mid = (lo_d + hi_d) // 2
        if feasible(deviation_edges(mid)) is None:
            best_d = mid
            hi_d = mid - 1
        else:
            lo_d = mid + 1

    # ---- 第二级：按人号顺序，逐人固定为最小可行整数值 ----
    fixed_edges: List[Edge] = []
    offsets: Dict[object, int] = {}

    for person in ordered:
        node = person_to_node[person]
        extra = deviation_edges(best_d) + fixed_edges

        # 可行域在单个变量上的投影是整数区间：
        #   x_node <= dist(ZERO -> node)，x_node >= -dist(node -> ZERO)。
        forward = _shortest_distances(base_edges + extra, node_count, reverse=False)
        backward = _shortest_distances(base_edges + extra, node_count, reverse=True)
        assert forward is not None and backward is not None

        low = max(lower_bounds[person], -backward[node])
        high = min(upper_bounds[person], forward[node], proposed[person] + best_d)
        low = max(low, proposed[person] - best_d)

        chosen: Optional[int] = None
        for candidate in range(low, high + 1):
            pin = [
                Edge(ZERO, node, candidate, ("fix", person)),
                Edge(node, ZERO, -candidate, ("fix", person)),
            ]
            if feasible(extra + pin) is None:
                chosen = candidate
                break
        if chosen is None:  # 理论上不可达：防御性断言
            raise RuntimeError("字典序固定阶段未找到可行值，求解器内部错误")

        offsets[person] = chosen
        fixed_edges.append(Edge(ZERO, node, chosen, ("fix", person)))
        fixed_edges.append(Edge(node, ZERO, -chosen, ("fix", person)))

    max_change = max(abs(offsets[p] - proposed[p]) for p in ordered) if ordered else 0
    return SolveResult(offsets=offsets, max_change=max_change)
