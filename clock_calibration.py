"""分布式链路追踪的时钟校准模块。

问题背景
--------
链路追踪中，一次发送与远端接收各自使用本机时钟打时间戳，图上可能把
接收画到发送之前。观测窗口内，每台主机 h 有一个固定的整数时钟偏移
offset[h]，满足：

    校准时间(主机 h 的时间戳 t) = t + offset[h]

每条链路观测给出：发送主机 A、接收主机 B、A 的本地发送时间戳 send_ts、
B 的本地接收时间戳 recv_ts，以及这次传递可能的最短、最长耗时
[min_delay, max_delay]。于是一次观测等价于差分约束：

    min_delay <= (recv_ts + offset[B]) - (send_ts + offset[A]) <= max_delay

即（令 c = recv_ts - send_ts）：

    min_delay - c <= offset[B] - offset[A] <= max_delay - c

指定一台基准主机偏移为零，联合全部观测，本模块求出每台主机相对基准的
最小、最大可行偏移（整数）。与基准不连通的主机标为无界；若某个不连通
小组内部自相矛盾，额外返回一组可复核矛盾的观测编号。

算法
----
差分约束系统用最短路求解。对约束 offset[v] - offset[u] <= w 建边
u -> v 权 w：

* 以基准为源点跑 Bellman-Ford，dist[ref][h] 即 offset[h] 的上界；
* 在反向图上以基准为源点再跑一次，得到 offset[h] 的下界
  （offset[h] >= -dist(h -> ref)）；
* 分量内存在负环 <=> 该连通分量的观测自相矛盾，从负环上取出经过的
  观测编号作为可复核的证物（witness）。

因为上界是“所有路径中最紧的一条”，所以两机直接观测很宽、但经第三台
中转能收紧的界，会自动反映在最短路结果里。

只使用标准库，Python 3.11。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Dict, Hashable, Iterable, List, Optional, Sequence, Tuple

__all__ = [
    "Observation",
    "HostBounds",
    "CalibrationResult",
    "calibrate",
    "verify_conflict",
]

_INF = float("inf")


@dataclass(frozen=True)
class Observation:
    """一条链路观测。

    sender / receiver : 发送、接收主机标识（任意可哈希对象，通常为主机名）。
    send_ts / recv_ts : 发送方、接收方各自本机时钟打下的整数时间戳。
    min_delay / max_delay : 本次传递可能的最短、最长耗时（整数，含端点）。
    """

    sender: Hashable
    receiver: Hashable
    send_ts: int
    recv_ts: int
    min_delay: int
    max_delay: int

    def __post_init__(self) -> None:
        if self.min_delay > self.max_delay:
            raise ValueError(
                f"min_delay ({self.min_delay}) > max_delay ({self.max_delay})"
            )


@dataclass(frozen=True)
class HostBounds:
    """某台主机相对基准的可行偏移区间（闭区间，整数）。"""

    min_offset: int
    max_offset: int


class CalibrationResult:
    """校准结果。

    属性
    ----
    reference : 基准主机标识（其偏移恒为 0）。
    bounds    : dict，主机 -> HostBounds。仅包含与基准连通且所在分量
                无矛盾的主机。
    unbounded : frozenset，与基准不连通、且所在分量自洽的主机（无界）。
    conflicted: frozenset，所在分量自相矛盾的主机（无可行偏移）。
    conflicts : tuple of tuple of int，每个矛盾分量对应一组观测编号
                （输入 observations 中的下标，升序），该子集单独看即
                不可满足，可用 verify_conflict 复核。
    """

    def __init__(
        self,
        reference: Hashable,
        bounds: Dict[Hashable, HostBounds],
        unbounded: Iterable[Hashable],
        conflicted: Iterable[Hashable],
        conflicts: Iterable[Tuple[int, ...]],
    ) -> None:
        self.reference = reference
        self.bounds = dict(bounds)
        self.unbounded = frozenset(unbounded)
        self.conflicted = frozenset(conflicted)
        self.conflicts = tuple(tuple(c) for c in conflicts)

    def __repr__(self) -> str:  # pragma: no cover - 便于调试
        return (
            f"CalibrationResult(reference={self.reference!r}, "
            f"bounds={self.bounds!r}, unbounded={sorted(self.unbounded)!r}, "
            f"conflicted={sorted(self.conflicted)!r}, "
            f"conflicts={self.conflicts!r})"
        )


# ---------------------------------------------------------------------------
# 内部：差分约束图
# ---------------------------------------------------------------------------

# 边: (u, v, w, obs_index) 表示约束 offset[v] - offset[u] <= w，来自第 obs_index 条观测
_Edge = Tuple[Hashable, Hashable, int, int]


def _build_edges(observations: Sequence[Observation]) -> List[_Edge]:
    edges: List[_Edge] = []
    for i, ob in enumerate(observations):
        c = ob.recv_ts - ob.send_ts
        upper = ob.max_delay - c  # offset[recv] - offset[send] <= upper
        lower = ob.min_delay - c  # offset[recv] - offset[send] >= lower
        edges.append((ob.sender, ob.receiver, upper, i))
        edges.append((ob.receiver, ob.sender, -lower, i))
    return edges


def _components(hosts: Iterable[Hashable], edges: Sequence[_Edge]) -> List[List[Hashable]]:
    """按无向连通性划分主机集合。"""
    adj: Dict[Hashable, List[Hashable]] = {h: [] for h in hosts}
    for u, v, _w, _i in edges:
        adj[u].append(v)
        adj[v].append(u)
    seen = set()
    comps: List[List[Hashable]] = []
    for h in adj:
        if h in seen:
            continue
        comp = []
        queue = deque([h])
        seen.add(h)
        while queue:
            x = queue.popleft()
            comp.append(x)
            for y in adj[x]:
                if y not in seen:
                    seen.add(y)
                    queue.append(y)
        comps.append(comp)
    return comps


def _find_negative_cycle(
    nodes: Sequence[Hashable], edges: Sequence[_Edge]
) -> Optional[List[int]]:
    """检测该分量内的负环；若有，返回环上经过的观测编号列表，否则 None。

    以全零距离初始化（等价于超级源点），因此只需 |V| 轮松弛即可判定
    分量内任意位置的负环。
    """
    dist = {n: 0 for n in nodes}
    pred: Dict[Hashable, Tuple[Hashable, int]] = {}
    n = len(nodes)
    last: Optional[Hashable] = None
    for _ in range(n):
        last = None
        for u, v, w, idx in edges:
            if dist[u] + w < dist[v]:
                dist[v] = dist[u] + w
                pred[v] = (u, idx)
                last = v
        if last is None:
            return None
    # 第 n 轮仍被松弛的点必能到达负环；沿前驱走 n 步必落在环上。
    x = last
    for _ in range(n):
        x = pred[x][0]
    witness: List[int] = []
    cur = x
    while True:
        prev, idx = pred[cur]
        witness.append(idx)
        cur = prev
        if cur == x:
            break
    return witness


def _shortest_paths(
    source: Hashable, nodes: Sequence[Hashable], edges: Sequence[_Edge]
) -> Dict[Hashable, int]:
    """单源最短路（调用方保证分量内无负环且所有点自 source 可达）。"""
    dist = {n: _INF for n in nodes}
    dist[source] = 0
    for _ in range(len(nodes) - 1):
        updated = False
        for u, v, w, _idx in edges:
            if dist[u] != _INF and dist[u] + w < dist[v]:
                dist[v] = dist[u] + w
                updated = True
        if not updated:
            break
    return dist  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------


def calibrate(
    observations: Sequence[Observation],
    reference: Hashable,
    hosts: Optional[Iterable[Hashable]] = None,
) -> CalibrationResult:
    """联合全部观测，求各主机相对基准的可行偏移区间。

    参数
    ----
    observations : Observation 序列；返回的观测编号即该序列中的下标（0 起）。
    reference    : 基准主机，偏移固定为 0。
    hosts        : 可选，额外纳入的主机（例如窗口内没有任何观测、但仍希望
                   出现在结果里的机器）。观测中出现的主机总是自动纳入。

    返回
    ----
    CalibrationResult，见该类文档。
    """
    all_hosts = set()
    for ob in observations:
        all_hosts.add(ob.sender)
        all_hosts.add(ob.receiver)
    all_hosts.add(reference)
    if hosts is not None:
        all_hosts.update(hosts)

    edges = _build_edges(observations)
    comps = _components(all_hosts, edges)

    bounds: Dict[Hashable, HostBounds] = {}
    unbounded: List[Hashable] = []
    conflicted: List[Hashable] = []
    conflicts: List[Tuple[int, ...]] = []

    for comp in comps:
        comp_set = set(comp)
        comp_edges = [e for e in edges if e[0] in comp_set]

        witness = _find_negative_cycle(comp, comp_edges)
        if witness is not None:
            conflicted.extend(comp)
            conflicts.append(tuple(sorted(set(witness))))
            continue

        if reference in comp_set:
            # 上界：offset[h] <= dist(ref -> h)
            upper = _shortest_paths(reference, comp, comp_edges)
            # 下界：offset[h] >= -dist(h -> ref)，在反向图上从 ref 求
            redges = [(v, u, w, i) for (u, v, w, i) in comp_edges]
            back = _shortest_paths(reference, comp, redges)
            for h in comp:
                bounds[h] = HostBounds(min_offset=-back[h], max_offset=upper[h])
        else:
            unbounded.extend(comp)

    return CalibrationResult(reference, bounds, unbounded, conflicted, conflicts)


def verify_conflict(
    observations: Sequence[Observation], indices: Iterable[int]
) -> bool:
    """复核一组观测编号是否确实自相矛盾（该子集单独看即不可满足）。

    对 calibrate 返回的每个 conflicts 元素应返回 True；对自洽子集返回
    False。编号越界时抛 IndexError。
    """
    idx_list = list(indices)
    sub = [observations[i] for i in idx_list]  # 越界自然抛 IndexError
    if not sub:
        return False
    nodes = set()
    for ob in sub:
        nodes.add(ob.sender)
        nodes.add(ob.receiver)
    edges = _build_edges(sub)
    return _find_negative_cycle(list(nodes), edges) is not None
