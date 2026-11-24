"""单时间段的分层直方图约束引擎与分位数极值计算。

数学模型
========
设某段未知的整数样本多重集取值于有限域 ``[lo, hi]``，前缀计数
``S(v) = #{样本 x : x <= v}``，并固定 ``S(lo-1) = 0``。
每个桶 ``[a,b]`` 计数为 c 等价于等式约束 ``S(b) - S(a-1) = c``，
样本非负等价于对所有相邻原子边界 p 有 ``S(p) >= S(前一节点)``。

所有约束均为形如 ``S(u) - S(v) <= d`` 的差分约束，约束矩阵全幺模，
因此：
* 可行（整数解）与否可用加权并查集合并等号、再用最长路检测正环判定；
* 任意边界点上 ``S(p)`` 的最小/最大可行值，即锚点到该点的最长/最短路；
* 第 k 小值的最小/最大可能值可在原子边界上一次扫描得到，
  并能据此构造以区间与重数表示的分配见证。
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .records import Observation, ValidationError


class InfeasibleError(Exception):
    """观测之间互相矛盾，不存在任何满足全部约束的样本多重集。"""

    def __init__(self, message: str = "观测约束矛盾，无可行样本分配"):
        super().__init__(message)
        self.message = message


class KOutOfRangeError(ValueError):
    """请求的秩 k 超出窗口样本总数范围。"""


# ---------------------------------------------------------------------------
# 加权并查集：维护 val[x] - val[parent[x]] = weight[x]
# ---------------------------------------------------------------------------
class _WeightedDSU:
    def __init__(self, nodes: Sequence[int]):
        self.parent: Dict[int, int] = {x: x for x in nodes}
        self.weight: Dict[int, int] = {x: 0 for x in nodes}
        self.rank: Dict[int, int] = {x: 0 for x in nodes}

    def find(self, x: int) -> Tuple[int, int]:
        """返回 (根, val[x] - val[根])，带路径压缩。"""
        if self.parent[x] == x:
            return x, 0
        root, w = self.find(self.parent[x])
        w += self.weight[x]
        self.parent[x] = root
        self.weight[x] = w
        return root, w

    def constrain_equal(self, u: int, v: int, diff: int) -> Optional[str]:
        """加入约束 val[u] - val[v] = diff。矛盾时返回原因字符串。"""
        ru, du = self.find(u)  # val[u] = val[ru] + du
        rv, dv = self.find(v)  # val[v] = val[rv] + dv
        if ru == rv:
            if du - dv != diff:
                return (
                    f"前缀和冲突: S({u}) - S({v}) 同时要求等于 "
                    f"{diff} 与 {du - dv}"
                )
            return None
        # 需要 val[ru] - val[rv] = diff - du + dv
        delta = diff - du + dv
        if self.rank[ru] < self.rank[rv]:
            self.parent[ru] = rv
            self.weight[ru] = delta
        elif self.rank[ru] > self.rank[rv]:
            self.parent[rv] = ru
            self.weight[rv] = -delta
        else:
            self.parent[rv] = ru
            self.weight[rv] = -delta
            self.rank[ru] += 1
        return None


# ---------------------------------------------------------------------------
# 见证相关数据结构
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RLE:
    """一段原子单元上的重数运行：单元 ``[cell_lo, cell_hi]`` 每个整数点放 count 个样本。"""

    cell_lo: int
    cell_hi: int
    count: int

    def to_dict(self) -> dict:
        return {"cell_lo": self.cell_lo, "cell_hi": self.cell_hi, "count": self.count}

    @classmethod
    def from_dict(cls, d: dict) -> "RLE":
        return cls(int(d["cell_lo"]), int(d["cell_hi"]), int(d["count"]))


@dataclass(frozen=True)
class Allocation:
    """单个时间段的分配见证，以区间与重数表示。"""

    source: str
    segment_start: int
    segment_length: int
    rle: Tuple[RLE, ...]

    def total(self) -> int:
        return sum(r.count * (r.cell_hi - r.cell_lo + 1) for r in self.rle)

    def multiplicity_at(self, v: int) -> int:
        total = 0
        for r in self.rle:
            if r.cell_lo <= v <= r.cell_hi:
                total += r.count
        return total

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "segment_start": self.segment_start,
            "segment_length": self.segment_length,
            "rle": [r.to_dict() for r in self.rle],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Allocation":
        return cls(
            source=d["source"],
            segment_start=int(d["segment_start"]),
            segment_length=int(d["segment_length"]),
            rle=tuple(RLE.from_dict(r) for r in d["rle"]),
        )


@dataclass(frozen=True)
class WitnessEntry:
    """桶式见证条目：整数闭区间 [lo, hi] 上每个值放置 multiplicity 个样本。"""

    lo: int
    hi: int
    multiplicity: int

    def __post_init__(self) -> None:
        if self.lo > self.hi or self.multiplicity < 0:
            raise ValidationError("见证条目非法")

    @property
    def total(self) -> int:
        return self.multiplicity * (self.hi - self.lo + 1)

    def to_dict(self) -> dict:
        return {"lo": self.lo, "hi": self.hi, "multiplicity": self.multiplicity}

    @classmethod
    def from_dict(cls, d: dict) -> "WitnessEntry":
        return cls(int(d["lo"]), int(d["hi"]), int(d["multiplicity"]))


@dataclass(frozen=True)
class Witness:
    """可独立验算的完整见证：内嵌其成立所依据的观测修订快照。"""

    source: str
    window_start: int
    window_end: int
    k: int
    bound: str  # "min" 或 "max"
    kth_value: int
    allocations: Tuple[Allocation, ...]
    observations: Tuple[Observation, ...]
    store_version: Optional[int]

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "k": self.k,
            "bound": self.bound,
            "kth_value": self.kth_value,
            "store_version": self.store_version,
            "allocations": [a.to_dict() for a in self.allocations],
            "observations": [o.to_dict() for o in self.observations],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Witness":
        return cls(
            source=d["source"],
            window_start=int(d["window_start"]),
            window_end=int(d["window_end"]),
            k=int(d["k"]),
            bound=d["bound"],
            kth_value=int(d["kth_value"]),
            allocations=tuple(Allocation.from_dict(a) for a in d["allocations"]),
            observations=tuple(
                Observation.from_dict(o) for o in d["observations"]
            ),
            store_version=d.get("store_version"),
        )


@dataclass(frozen=True)
class MusReport:
    """不可行报告：极小矛盾观测子集（删任一条后剩余集合均可行）。"""

    source: str
    segment_start: int
    segment_length: int
    mus: Tuple[Observation, ...]
    reason: str

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "segment_start": self.segment_start,
            "segment_length": self.segment_length,
            "reason": self.reason,
            "mus": [
                {
                    "series_id": o.series_id,
                    "revision": o.revision,
                    "observation": o.to_dict(),
                }
                for o in self.mus
            ],
        }


@dataclass
class WindowResult:
    """一次窗口查询的结果。

    可行时 ``status == "ok"``，给出第 k 小的最小/最大可能值及两份见证；
    不可行时 ``status == "infeasible"``，``mus_reports`` 列出各矛盾段的 MUS。
    """

    status: str
    source: str
    window_start: int
    window_end: int
    k: Optional[int] = None
    total_samples: Optional[int] = None
    min_kth: Optional[int] = None
    max_kth: Optional[int] = None
    min_witness: Optional[Witness] = None
    max_witness: Optional[Witness] = None
    mus_reports: List[MusReport] = field(default_factory=list)
    store_version: Optional[int] = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"


# ---------------------------------------------------------------------------
# 单段约束模型
# ---------------------------------------------------------------------------
class SegmentModel:
    """一个源、一个时间段上全部有效观测的联合约束模型。"""

    def __init__(self, observations: Sequence[Observation]):
        if not observations:
            raise ValidationError("该时间段没有任何观测")
        first = observations[0]
        self.source = first.source
        self.segment_start = first.segment_start
        self.segment_length = first.segment_length
        for o in observations:
            if (
                o.source != self.source
                or o.segment_start != self.segment_start
                or o.segment_length != self.segment_length
            ):
                raise ValidationError("观测不属于同一源/同一时间段")
            if o.lo != first.lo or o.hi != first.hi:
                raise ValidationError(
                    f"同一时间段的值域必须一致: "
                    f"[{o.lo},{o.hi}] vs [{first.lo},{first.hi}]"
                )
        self.observations: Tuple[Observation, ...] = tuple(observations)
        self.lo = first.lo
        self.hi = first.hi
        self.anchor = self.lo - 1
        self.feasible: bool = True
        self.reason: Optional[str] = None
        self.s_min: Optional[Dict[int, int]] = None
        self.s_max: Optional[Dict[int, int]] = None
        self.total: int = 0

        # 收集所有前缀和节点：锚点与每个桶的右端、每个桶左端的前一点
        nodes = {self.anchor}
        for o in self.observations:
            for b in o.buckets:
                nodes.add(b.hi)
                nodes.add(b.lo - 1)
        self.points: List[int] = sorted(nodes)  # 含 anchor，严格递增
        index = {p: i for i, p in enumerate(self.points)}

        dsu = _WeightedDSU(self.points)
        for o in self.observations:
            for b in o.buckets:
                err = dsu.constrain_equal(b.hi, b.lo - 1, b.count)
                if err is not None:
                    self.feasible = False
                    self.reason = f"系列 {o.series_id} rev {o.revision}: {err}"
                    self.s_min = self.s_max = None
                    self._index = index
                    return
            # 整段总数：S(hi) - S(anchor) = total
            err = dsu.constrain_equal(self.hi, self.anchor, o.total)
            if err is not None:
                self.feasible = False
                self.reason = (
                    f"系列 {o.series_id} rev {o.revision}: 样本总数冲突 "
                    f"({err})"
                )
                self.s_min = self.s_max = None
                self._index = index
                return

        # 单调边：相邻前缀节点 S(p_j) - S(p_{j-1}) >= 0
        # 组件间最长路图，边 u -> v 权 d 表示 S(v) >= S(u) + d
        # 先对连通组件紧凑编号 0..C-1
        root_to_comp: Dict[int, int] = {}
        comp_of_point: Dict[int, int] = {}
        for p in self.points:
            r, _ = dsu.find(p)
            if r not in root_to_comp:
                root_to_comp[r] = len(root_to_comp)
            comp_of_point[p] = root_to_comp[r]
        edges: List[Tuple[int, int, int]] = []
        for j in range(1, len(self.points)):
            u = comp_of_point[self.points[j - 1]]
            v = comp_of_point[self.points[j]]
            if u != v:
                edges.append((u, v, 0))

        anchor_root, _ = dsu.find(self.anchor)
        ar = root_to_comp[anchor_root]
        n_comp = len(root_to_comp)

        # 各组件内的相对偏移：find 返回 w = val[p] - val[root]
        offset: Dict[int, int] = {}
        for p in self.points:
            _, w = dsu.find(p)
            offset[p] = w
        # 锚点组件的根节点相对锚点的偏移：S(anchor) = root_val + w_anchor = 0
        # 因此该组件根值必须为 -w_anchor
        anchor_w = offset[self.anchor]

        if not edges:
            # 全部前缀节点被等式连成一个组件，根值被锚点唯一固定
            dist_min = [0] * n_comp
            dist_max = [0] * n_comp
            dist_min[ar] = dist_max[ar] = -anchor_w
        else:
            dist_min, cycle = _bellman_ford_longest(n_comp, edges, ar)
            if cycle:
                self.feasible = False
                self.reason = "单调性差分约束出现正环，单元计数无法同时非负"
                self.s_min = self.s_max = None
                self._index = index
                return
            # Bellman-Ford 给出的是相对锚点组件根的增量，
            # 锚点组件根本身取固定值 -anchor_w
            if any(d is None for d in dist_min):
                self.feasible = False
                self.reason = "存在无法从锚点到达的前缀组件，约束不完整"
                self.s_min = self.s_max = None
                self._index = index
                return
            dist_min = [d + (-anchor_w) for d in dist_min]  # type: ignore[operator]

            # 最大可行值：max y_v = 原图上 v 到锚点的最长路
            # 等价于反向图上锚点到 v 的最短路
            rev_edges = [(v, u, d) for (u, v, d) in edges]
            dist_max, cycle2 = _bellman_ford_shortest(n_comp, rev_edges, ar)
            assert not cycle2
            if any(d is None for d in dist_max):
                self.feasible = False
                self.reason = "存在无法到达锚点的前缀组件，约束不完整"
                self.s_min = self.s_max = None
                self._index = index
                return
            dist_max = [d + (-anchor_w) for d in dist_max]  # type: ignore[operator]

        s_min: Dict[int, int] = {}
        s_max: Dict[int, int] = {}
        for p in self.points:
            cr = comp_of_point[p]
            s_min[p] = dist_min[cr] + offset[p]
            s_max[p] = dist_max[cr] + offset[p]
        # 锚点固定为 0
        assert s_min[self.anchor] == 0 and s_max[self.anchor] == 0
        self.feasible = True
        self.s_min = s_min
        self.s_max = s_max
        self._index = index
        self.total = s_min[self.hi]
        assert self.total == s_max[self.hi]
        assert self.total >= 0

    @property
    def segment_end(self) -> int:
        return self.segment_start + self.segment_length

    # ------------------------------------------------------------------
    # 前缀和极值查询
    # ------------------------------------------------------------------
    def _check_feasible(self) -> None:
        if not self.feasible:
            raise InfeasibleError(self.reason)

    def prefix_bounds(self, v: int) -> Tuple[int, int]:
        """返回可行域上 (min S(v), max S(v))，v 可为域内任意整数。"""
        self._check_feasible()
        if v <= self.anchor:
            if v < self.anchor:
                raise ValidationError(f"查询点 {v} 超出值域")
            return 0, 0
        if v >= self.hi:
            return self.total, self.total
        points = self.points
        j = bisect_right(points, v) - 1  # points[j] <= v
        left = points[j]
        if left == v:
            return self.s_min[v], self.s_max[v]
        # v 严格落在原子单元 (left, points[j+1]) 内：
        # 单元内样本可任意布置，故 S(v) ∈ [S(left), S(下一边界)]
        return self.s_min[left], self.s_max[points[j + 1]]

    def cell_counts(self, maximize: bool) -> List[int]:
        """极值解在每个原子单元上的样本计数。

        maximize=True 取组件级最大可行前缀和（用于最小 kth 见证），
        否则取最小可行前缀和（用于最大 kth 见证）。
        """
        self._check_feasible()
        table = self.s_max if maximize else self.s_min
        counts = []
        prev = 0
        for p in self.points[1:]:
            cur = table[p]
            counts.append(cur - prev)
            prev = cur
        return counts

    # ------------------------------------------------------------------
    # 见证构造
    # ------------------------------------------------------------------
    def build_allocation(self, bound: str) -> Allocation:
        """构造达到 min/max 第 k 小边界所需的极值分配。

        每个原子单元的样本集中在单一整数点上：
        bound="min" 时贴单元左边界（前缀和最大解），
        bound="max" 时贴单元右边界（前缀和最小解）。
        RLE 段因此退化为单点区间 [v,v]，重数即该单元样本数。
        """
        self._check_feasible()
        maximize = bound == "min"  # 最小 kth <=> 前缀和最大解
        counts = self.cell_counts(maximize)
        rle: List[RLE] = []
        for i, c in enumerate(counts):
            if c == 0:
                continue
            if maximize:
                v = self.points[i] + 1  # 单元左边界
            else:
                v = self.points[i + 1]  # 单元右边界
            rle.append(RLE(cell_lo=v, cell_hi=v, count=c))
        return Allocation(
            source=self.source,
            segment_start=self.segment_start,
            segment_length=self.segment_length,
            rle=tuple(rle),
        )

    def allocation_to_witness_entries(
        self, alloc: Allocation, place: str
    ) -> Tuple[WitnessEntry, ...]:
        """把极值分配落实为具体整数点重数（区间与重数见证）。

        place="left"：每个单元的样本全部放在单元最小整数（达到最小 kth）；
        place="right"：全部放在单元最大整数（达到最大 kth）。
        """
        entries: List[WitnessEntry] = []
        for r in alloc.rle:
            v = r.cell_lo if place == "left" else r.cell_hi
            entries.append(WitnessEntry(lo=v, hi=v, multiplicity=r.count))
        return tuple(entries)


def _bellman_ford_longest(
    n: int, edges: Sequence[Tuple[int, int, int]], source: int
) -> Tuple[List[Optional[int]], bool]:
    """最长路（SPFA/Bellman-Ford）。返回 (距离, 是否存在可达正环)。

    距离用 ``None`` 表示不可达，避免大整数计数撞上固定哨兵。
    """
    from collections import deque

    adj: Dict[int, List[Tuple[int, int]]] = {i: [] for i in range(n)}
    for u, v, d in edges:
        adj[u].append((v, d))
    dist: List[Optional[int]] = [None] * n
    dist[source] = 0
    in_queue = [False] * n
    relax_count = [0] * n
    q = deque([source])
    in_queue[source] = True
    while q:
        u = q.popleft()
        in_queue[u] = False
        base = dist[u]
        assert base is not None
        for v, d in adj[u]:
            cand = base + d
            if dist[v] is None or cand > dist[v]:
                dist[v] = cand
                relax_count[v] += 1
                if relax_count[v] >= n:
                    return dist, True
                if not in_queue[v]:
                    q.append(v)
                    in_queue[v] = True
    return dist, False


def _bellman_ford_shortest(
    n: int, edges: Sequence[Tuple[int, int, int]], source: int
) -> Tuple[List[Optional[int]], bool]:
    """最短路（SPFA）。返回 (距离, 是否存在可达负环)，不可达为 ``None``。"""
    from collections import deque

    adj: Dict[int, List[Tuple[int, int]]] = {i: [] for i in range(n)}
    for u, v, d in edges:
        adj[u].append((v, d))
    dist: List[Optional[int]] = [None] * n
    dist[source] = 0
    in_queue = [False] * n
    relax_count = [0] * n
    q = deque([source])
    in_queue[source] = True
    while q:
        u = q.popleft()
        in_queue[u] = False
        base = dist[u]
        assert base is not None
        for v, d in adj[u]:
            cand = base + d
            if dist[v] is None or cand < dist[v]:
                dist[v] = cand
                relax_count[v] += 1
                if relax_count[v] >= n:
                    return dist, True
                if not in_queue[v]:
                    q.append(v)
                    in_queue[v] = True
    return dist, False
