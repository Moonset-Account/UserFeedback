"""领域模型：双资源节点、带现址的 Pod、约束对与拓扑。

所有数量（CPU / 内存 / 副本数）都使用非负整数。节点数量上限 10、
Pod 数量上限 18，超出会抛出 :class:`PlanningError`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

MAX_NODES = 10
MAX_PODS = 18


class PlanningError(ValueError):
    """输入非法或计划在当前拓扑下不可执行。"""


class ExplorationLimit(RuntimeError):
    """搜索超过调用方给出的状态/动作预算。"""


@dataclass(frozen=True)
class Resources:
    """CPU 与内存的双资源量，支持加、减与容量比较。"""

    cpu: int = 0
    mem: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.cpu, int) or not isinstance(self.mem, int):
            raise PlanningError("资源量必须是整数")
        if self.cpu < 0 or self.mem < 0:
            raise PlanningError("资源量不能为负")

    def __add__(self, other: "Resources") -> "Resources":
        return Resources(self.cpu + other.cpu, self.mem + other.mem)

    def __sub__(self, other: "Resources") -> "Resources":
        return Resources(self.cpu - other.cpu, self.mem - other.mem)

    def fits_in(self, capacity: "Resources") -> bool:
        return self.cpu <= capacity.cpu and self.mem <= capacity.mem


class PairType(str, Enum):
    """Pod 对约束：必须同机 / 必须异机。"""

    SAME_NODE = "same_node"
    DIFFERENT_NODE = "different_node"


@dataclass(frozen=True)
class Pod:
    """一个 Pod 副本。

    workload 用于按工作负载做区域副本差统计；node 是初始现址，
    ``node is None`` 表示等待接入（仅用于测试构造，正式输入均有现址）。
    selector 中的标签要求目的地节点全部具备。
    """

    id: str
    workload: str
    request: Resources
    node: str | None = None
    immovable: bool = False
    selector: frozenset[tuple[str, str]] = frozenset()

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id:
            raise PlanningError("Pod id 必须是非空字符串")
        if not isinstance(self.workload, str) or not self.workload:
            raise PlanningError("workload 必须是非空字符串")
        if not isinstance(self.selector, frozenset):
            object.__setattr__(self, "selector", frozenset(self.selector))


@dataclass(frozen=True)
class Node:
    """一台双资源节点。

    cordoned 为 True 表示软下线：仍可释放旧副本，但不能接入新副本。
    labels 是节点自带标签；zone 决定区域副本差统计的归属。
    """

    id: str
    capacity: Resources
    zone: str
    labels: frozenset[tuple[str, str]] = frozenset()
    cordoned: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id:
            raise PlanningError("节点 id 必须是非空字符串")
        if not isinstance(self.zone, str) or not self.zone:
            raise PlanningError("zone 必须是非空字符串")
        if not isinstance(self.labels, frozenset):
            object.__setattr__(self, "labels", frozenset(self.labels))


@dataclass(frozen=True)
class PodPair:
    """一对 Pod 之间的同机/异机约束。"""

    a: str
    b: str
    kind: PairType


@dataclass
class Topology:
    """一次规划的完整静态输入。

    zone_delta_caps 按 workload 给出任意两个区域之间允许的副本数差上限；
    未列出的 workload 不做区域差限制。
    """

    nodes: list[Node]
    pods: list[Pod]
    pairs: list[PodPair] = field(default_factory=list)
    zone_delta_caps: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if len(self.nodes) > MAX_NODES:
            raise PlanningError(f"节点数超过上限 {MAX_NODES}")
        if len(self.pods) > MAX_PODS:
            raise PlanningError(f"Pod 数超过上限 {MAX_PODS}")
        ids = [n.id for n in self.nodes]
        if len(set(ids)) != len(ids):
            raise PlanningError("节点 id 重复")
        pod_ids = [p.id for p in self.pods]
        if len(set(pod_ids)) != len(pod_ids):
            raise PlanningError("Pod id 重复")
        node_set = set(ids)
        for pod in self.pods:
            if pod.node is not None and pod.node not in node_set:
                raise PlanningError(f"Pod {pod.id} 的现址 {pod.node} 不存在")
        known = set(pod_ids)
        for pair in self.pairs:
            if pair.a not in known or pair.b not in known or pair.a == pair.b:
                raise PlanningError(f"非法 Pod 约束对 {pair}")
        for workload, cap in self.zone_delta_caps.items():
            if not isinstance(cap, int) or cap < 0:
                raise PlanningError("区域差上限必须是非负整数")
            if workload not in {p.workload for p in self.pods}:
                raise PlanningError(f"区域差上限引用了未知 workload {workload}")

    # ---- 便捷索引 -----------------------------------------------------
    @property
    def node_map(self) -> dict[str, Node]:
        return {n.id: n for n in self.nodes}

    @property
    def pod_map(self) -> dict[str, Pod]:
        return {p.id: p for p in self.pods}

    def destinations_for(self, pod: Pod) -> list[str]:
        """满足标签选择器且未软下线的候选节点（含源节点，搜索时会跳过）。"""
        out: list[str] = []
        for node in self.nodes:
            if node.cordoned:
                continue
            if pod.selector and not pod.selector.issubset(node.labels):
                continue
            out.append(node.id)
        return out


@dataclass(frozen=True)
class Action:
    """一次搬迁：把 clique 中的全部 Pod 从 source 整体搬到 destination。

    clique 是同机约束闭包。单个 Pod 搬迁时 clique 只含自身；
    多个必须同机的 Pod 只能整体搬迁，保证中间稳定态不违反同机约束。
    ticket 由规划器/回放器按动作顺序编号（T1、T2 ...）。
    """

    source: str
    destination: str
    clique: tuple[str, ...]
    ticket: str = ""

    def __post_init__(self) -> None:
        if self.source == self.destination:
            raise PlanningError("搬迁的源与目的地不能相同")
        if len(self.clique) != len(set(self.clique)) or not self.clique:
            raise PlanningError("搬迁 clique 必须是非空且无重复的 Pod 序列")


@dataclass(frozen=True)
class PlanCost:
    """方案成本：先比退役节点数（外部驱动），再比搬迁次数，再比动作序。"""

    moves: int

    def key(self) -> tuple[int, ...]:
        return (self.moves,)


@dataclass(frozen=True)
class Plan:
    """可逐步回放的缩容方案。

    retiring 是本方案承诺退役的节点集合；actions 是两阶段搬迁动作序列。
    """

    retiring: frozenset[str]
    actions: tuple[Action, ...]
    moves: int

    @property
    def cost(self) -> PlanCost:
        return PlanCost(self.moves)
