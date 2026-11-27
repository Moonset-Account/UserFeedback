"""静态分析的中间表示（IR）：控制流图节点、程序与构建器。

节点种类
--------
- :class:`EntryNode`        程序入口，可声明初始变量绑定。
- :class:`BranchNode`       条件分支，拥有两条出边（true / false）。
- :class:`MergeNode`        显式合流点；任何拥有多个前驱的节点也按合流处理。
- :class:`AuthCheckNode`    一次成功的授权检查，产生
                            ``(对象, 版本, 操作)`` 授权事实。
- :class:`AliasNode`        变量别名或对象副本，同时可推进版本。
- :class:`CallNode`         服务调用，摘要注明替换哪些对象 / 推进哪些版本，
                            以及是否为需要授权约束的写入位置。
- :class:`TxBeginNode` / :class:`TxCommitNode` / :class:`TxRollbackNode`
  可选的事务边界节点。

程序只允许是*无环*控制流图（DAG）；循环会触发
:class:`~authflow.errors.ProgramStructureError`。这是因为授权事实携带的是
"必须在所有到达路径上成立"的 must 语义，且版本会随调用单调推进，循环需要
额外的 widening 处理，当前实现明确不覆盖该场景。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

from authflow.errors import ProgramStructureError

__all__ = [
    "Node",
    "EntryNode",
    "BranchNode",
    "MergeNode",
    "AuthCheckNode",
    "AliasNode",
    "CallNode",
    "TxBeginNode",
    "TxCommitNode",
    "TxRollbackNode",
    "Program",
    "ProgramBuilder",
]


@dataclass(frozen=True)
class Node:
    """所有 CFG 节点的基类。

    Attributes:
        node_id: 程序内唯一标识。
    """

    node_id: str
    kind: ClassVar[str] = "node"


@dataclass(frozen=True)
class EntryNode(Node):
    """程序入口。

    Args:
        initial_bindings: 初始变量绑定，形如 ``{"order": ("oid-1", 1)}``，
            值为 ``(对象标识, 版本)``。
    """

    initial_bindings: dict[str, tuple[str, int]] = field(default_factory=dict)
    kind: ClassVar[str] = "entry"


@dataclass(frozen=True)
class BranchNode(Node):
    """条件分支。出边标签固定为 ``"true"`` 与 ``"false"``。"""

    condition: str = ""
    kind: ClassVar[str] = "branch"


@dataclass(frozen=True)
class MergeNode(Node):
    """显式合流点（可无操作，仅用于标记路径重新汇合）。"""

    label: str = ""
    kind: ClassVar[str] = "merge"


@dataclass(frozen=True)
class AuthCheckNode(Node):
    """一次成功的授权检查。

    检查成功后产生事实 ``(object_id, version, operation)``。版本通常以
    ``-1`` 表示"检查时变量所指对象的当前版本"（延迟绑定），也可给出具体
    版本号做显式版本断言。

    Args:
        object_id: 被授权的对象标识。
        version: 授权覆盖的具体版本；``-1`` 表示取 ``target`` 当前版本。
        operation: 授权的操作名（如 ``"write"``、``"delete"``）。
        target: 可选，被检查的变量名，用于版本延迟绑定。
    """

    object_id: str
    version: int
    operation: str
    target: str | None = None
    kind: ClassVar[str] = "auth_check"


@dataclass(frozen=True)
class AliasNode(Node):
    """变量别名 / 对象副本 / 版本推进节点。

    语义由 ``is_copy`` 区分：

    - ``is_copy=False``：``lhs`` 成为 ``rhs`` 的*别名*，二者指向同一对象
      与版本（引用共享），别名上的授权事实互通。
    - ``is_copy=True``：``lhs`` 得到 ``rhs`` 对象的一份*副本*，副本是新
      对象（``new_object_id``），授权事实不会复制到副本上。

    两种情况下 ``new_version`` 都可把 lhs 推进到指定版本；为 ``None`` 时
    沿用 rhs 当前版本。
    """

    lhs: str
    rhs: str
    is_copy: bool = False
    new_object_id: str | None = None
    new_version: int | None = None
    kind: ClassVar[str] = "alias"


@dataclass(frozen=True)
class CallNode(Node):
    """服务调用节点，携带预计算的调用摘要。

    摘要字段（均为可选）：

    - ``target``：调用作用的变量名；写入校验针对该变量当前绑定进行。
    - ``operation``：调用执行的操作名，与授权事实的 operation 匹配。
    - ``is_write``：是否为持久化写入位置（需要授权约束才安全）。
    - ``replaces``：被整对象替换的对象标识集合；替换后这些对象旧版本上的
      全部授权事实失效。
    - ``advances``：版本推进映射 ``对象标识 -> 新版本``；推进后该对象旧
      版本上的事实失效。
    - ``rebindings``：调用后变量的新绑定 ``变量 -> (对象标识, 版本)``，
      用于模拟调用返回并替换句柄（替换来源默认视为未经授权的外部来源）。
    - ``unbinds``：调用后失去绑定的变量集合。
    """

    target: str | None = None
    operation: str | None = None
    is_write: bool = False
    replaces: frozenset[str] = frozenset()
    advances: dict[str, int] = field(default_factory=dict)
    rebindings: dict[str, tuple[str, int]] = field(default_factory=dict)
    unbinds: frozenset[str] = frozenset()
    kind: ClassVar[str] = "call"


@dataclass(frozen=True)
class TxBeginNode(Node):
    """事务开始节点，开启一个可嵌套的事务作用域。"""

    kind: ClassVar[str] = "tx_begin"


@dataclass(frozen=True)
class TxCommitNode(Node):
    """事务提交节点，当前最内层事务的事实与版本变更被保留。"""

    kind: ClassVar[str] = "tx_commit"


@dataclass(frozen=True)
class TxRollbackNode(Node):
    """事务回滚节点。

    回滚恢复最近一个 :class:`TxBeginNode` 时的绑定/版本/事实快照，事务
    作用域内产生或改写的授权事实全部进入失效记录。
    """

    kind: ClassVar[str] = "tx_rollback"


class Program:
    """由节点与有向边构成的控制流图（要求为 DAG）。"""

    def __init__(
        self,
        nodes: dict[str, Node],
        edges: list[tuple[str, str, str]],
        entry_id: str,
    ) -> None:
        self.nodes: dict[str, Node] = dict(nodes)
        # 邻接表：node_id -> [(successor_id, edge_label)]
        self.successors: dict[str, list[tuple[str, str]]] = {
            nid: [] for nid in nodes
        }
        # 前驱表：node_id -> [(predecessor_id, edge_label)]
        self.predecessors: dict[str, list[tuple[str, str]]] = {
            nid: [] for nid in nodes
        }
        for src, dst, label in edges:
            self.successors[src].append((dst, label))
            self.predecessors[dst].append((src, label))
        self.entry_id: str = entry_id

    def successors_of(self, node_id: str) -> list[tuple[str, str]]:
        return list(self.successors[node_id])

    def predecessors_of(self, node_id: str) -> list[tuple[str, str]]:
        return list(self.predecessors[node_id])

    def topological_order(self) -> list[str]:
        """返回入口可达子图上的 Kahn 拓扑序。

        不可达节点（构造期允许存在的孤岛）不参与排序，也不影响可达节点
        的入度；可达子图内存在环时抛出
        :class:`~authflow.errors.ProgramStructureError`。
        """
        reachable = self.reachable_from_entry()
        indegree = {
            nid: sum(
                1
                for pred, _label in self.predecessors[nid]
                if pred in reachable
            )
            for nid in reachable
        }
        queue = [nid for nid, deg in indegree.items() if deg == 0]
        order: list[str] = []
        while queue:
            # 排序保证结果确定性
            queue.sort()
            current = queue.pop(0)
            order.append(current)
            for dst, _label in self.successors[current]:
                if dst not in reachable:
                    continue
                indegree[dst] -= 1
                if indegree[dst] == 0:
                    queue.append(dst)
        if len(order) != len(reachable):
            cyclic = sorted(
                nid for nid, deg in indegree.items() if deg > 0
            )
            raise ProgramStructureError(
                f"控制流图存在环，当前分析仅支持 DAG；环上节点: {cyclic}"
            )
        return order

    def reachable_from_entry(self) -> frozenset[str]:
        """从入口沿 CFG 可达的全部节点标识。"""
        seen: set[str] = set()
        stack = [self.entry_id]
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            for dst, _label in self.successors[current]:
                stack.append(dst)
        return frozenset(seen)


class ProgramBuilder:
    """以流式 API 构建 :class:`Program`，并做结构校验。"""

    def __init__(self) -> None:
        self._nodes: dict[str, Node] = {}
        self._edges: list[tuple[str, str, str]] = []
        self._entry_id: str | None = None

    # ---- 节点 ----------------------------------------------------------
    def add_node(self, node: Node) -> "ProgramBuilder":
        if node.node_id in self._nodes:
            raise ProgramStructureError(f"节点标识重复: {node.node_id!r}")
        self._nodes[node.node_id] = node
        if isinstance(node, EntryNode):
            if self._entry_id is not None:
                raise ProgramStructureError("程序只能有一个入口节点")
            self._entry_id = node.node_id
        return self

    def entry(
        self,
        node_id: str,
        initial_bindings: dict[str, tuple[str, int]] | None = None,
    ) -> "ProgramBuilder":
        return self.add_node(
            EntryNode(
                node_id=node_id,
                initial_bindings=dict(initial_bindings or {}),
            )
        )

    def branch(self, node_id: str, condition: str = "") -> "ProgramBuilder":
        return self.add_node(BranchNode(node_id, condition=condition))

    def merge(self, node_id: str, label: str = "") -> "ProgramBuilder":
        return self.add_node(MergeNode(node_id, label=label))

    def auth_check(
        self,
        node_id: str,
        object_id: str,
        operation: str,
        version: int = -1,
        target: str | None = None,
    ) -> "ProgramBuilder":
        return self.add_node(
            AuthCheckNode(
                node_id=node_id,
                object_id=object_id,
                version=version,
                operation=operation,
                target=target,
            )
        )

    def alias(
        self,
        node_id: str,
        lhs: str,
        rhs: str,
        *,
        is_copy: bool = False,
        new_object_id: str | None = None,
        new_version: int | None = None,
    ) -> "ProgramBuilder":
        if is_copy and new_object_id is None:
            raise ProgramStructureError(
                f"副本节点 {node_id!r} 必须提供 new_object_id"
            )
        return self.add_node(
            AliasNode(
                node_id=node_id,
                lhs=lhs,
                rhs=rhs,
                is_copy=is_copy,
                new_object_id=new_object_id,
                new_version=new_version,
            )
        )

    def call(
        self,
        node_id: str,
        *,
        target: str | None = None,
        operation: str | None = None,
        is_write: bool = False,
        replaces: frozenset[str] | set[str] | tuple[str, ...] = (),
        advances: dict[str, int] | None = None,
        rebindings: dict[str, tuple[str, int]] | None = None,
        unbinds: frozenset[str] | set[str] | tuple[str, ...] = (),
    ) -> "ProgramBuilder":
        return self.add_node(
            CallNode(
                node_id=node_id,
                target=target,
                operation=operation,
                is_write=is_write,
                replaces=frozenset(replaces),
                advances=dict(advances or {}),
                rebindings=dict(rebindings or {}),
                unbinds=frozenset(unbinds),
            )
        )

    def tx_begin(self, node_id: str) -> "ProgramBuilder":
        return self.add_node(TxBeginNode(node_id))

    def tx_commit(self, node_id: str) -> "ProgramBuilder":
        return self.add_node(TxCommitNode(node_id))

    def tx_rollback(self, node_id: str) -> "ProgramBuilder":
        return self.add_node(TxRollbackNode(node_id))

    # ---- 边 ------------------------------------------------------------
    def connect(
        self, src: str, dst: str, label: str = "flow"
    ) -> "ProgramBuilder":
        self._edges.append((src, dst, label))
        return self

    def branch_edges(
        self, branch_id: str, true_id: str, false_id: str
    ) -> "ProgramBuilder":
        """为分支节点一次性添加 true / false 两条出边。"""
        self._edges.append((branch_id, true_id, "true"))
        self._edges.append((branch_id, false_id, "false"))
        return self

    # ---- 构建 ----------------------------------------------------------
    def build(self) -> Program:
        if self._entry_id is None:
            raise ProgramStructureError("程序缺少入口节点")
        for src, dst, _label in self._edges:
            if src not in self._nodes:
                raise ProgramStructureError(f"边引用了不存在的源节点: {src!r}")
            if dst not in self._nodes:
                raise ProgramStructureError(f"边引用了不存在的目标节点: {dst!r}")
        branch_ids = {
            nid
            for nid, node in self._nodes.items()
            if isinstance(node, BranchNode)
        }
        out_labels: dict[str, set[str]] = {}
        for src, _dst, label in self._edges:
            out_labels.setdefault(src, set()).add(label)
        for bid in branch_ids:
            labels = out_labels.get(bid, set())
            if labels != {"true", "false"}:
                raise ProgramStructureError(
                    f"分支节点 {bid!r} 必须恰有 true/false 两条出边，"
                    f"实际为: {sorted(labels)}"
                )
        program = Program(dict(self._nodes), list(self._edges), self._entry_id)
        # 提前做环检测，让 build() 阶段即暴露不支持的结构
        program.topological_order()
        return program
