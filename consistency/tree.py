"""地区树数据结构。

树节点带三个输入字段：
    noisy           该节点可公开的带噪整数（可为负，加噪结果不保证非负）
    weight          正整数置信权重
    structural_zero 是否为结构性零叶子（该叶子计数恒为 0）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable


@dataclass
class Node:
    """地区树节点。

    属性：
        node_id:         节点唯一编号（字符串）
        noisy:           带噪整数计数
        weight:          正整数置信权重
        structural_zero: 仅允许对叶子声明；为 True 时该叶子校正后恒为 0
        children:        子节点列表
    """

    node_id: str
    noisy: int
    weight: int
    structural_zero: bool = False
    children: list["Node"] = field(default_factory=list)

    def is_leaf(self) -> bool:
        return not self.children

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return f"Node({self.node_id!r}, noisy={self.noisy}, w={self.weight})"


@dataclass(frozen=True)
class RegionTree:
    """一棵经过校验的地区树。

    属性：
        root:       根节点
        nodes:      node_id -> Node 的映射
        leaves:     按 DFS 先序（即题目中的叶编号顺序）排列的叶子列表
        depth:      node_id -> 深度（根为 0）
        parent:     node_id -> 父 Node（根的父为 None）
    """

    root: Node
    nodes: dict[str, Node]
    leaves: list[Node]
    depth: dict[str, int]
    parent: dict[str, Node | None]

    @classmethod
    def build(cls, root: Node) -> "RegionTree":
        """从根节点构建并校验地区树。

        校验内容：编号为非空字符串且唯一；noisy 为整数；weight 为正整数；
        structural_zero 只出现在叶子；树连通且无环、无悬挂引用。
        """

        if not isinstance(root, Node):
            raise TypeError("root 必须是 Node 实例")

        nodes: dict[str, Node] = {}
        leaves: list[Node] = []
        depth: dict[str, int] = {}
        parent: dict[str, Node | None] = {}

        def visit(node: object, par: Node | None, d: int) -> None:
            if not isinstance(node, Node):
                raise TypeError("children 中的元素必须是 Node 实例")
            if not isinstance(node.node_id, str) or not node.node_id:
                raise ValueError("node_id 必须是非空字符串")
            if node.node_id in nodes:
                raise ValueError(f"节点编号重复: {node.node_id!r}")
            if isinstance(node.noisy, bool) or not isinstance(node.noisy, int):
                raise TypeError(f"节点 {node.node_id!r} 的 noisy 必须是整数")
            if isinstance(node.weight, bool) or not isinstance(node.weight, int):
                raise TypeError(f"节点 {node.node_id!r} 的 weight 必须是整数")
            if node.weight <= 0:
                raise ValueError(f"节点 {node.node_id!r} 的 weight 必须为正整数")
            if not isinstance(node.structural_zero, bool):
                raise TypeError(
                    f"节点 {node.node_id!r} 的 structural_zero 必须是布尔值"
                )
            if not isinstance(node.children, list):
                raise TypeError(f"节点 {node.node_id!r} 的 children 必须是 list")

            nodes[node.node_id] = node
            depth[node.node_id] = d
            parent[node.node_id] = par

            if node.is_leaf():
                leaves.append(node)
            elif node.structural_zero:
                raise ValueError(
                    f"结构性零只能声明在叶子上，节点 {node.node_id!r} 不是叶子"
                )

            for child in node.children:
                visit(child, node, d + 1)

        visit(root, None, 0)
        return cls(root=root, nodes=nodes, leaves=leaves, depth=depth, parent=parent)

    def ancestors_of_leaf(self, leaf: Node) -> list[Node]:
        """返回叶子到根路径上的节点（含叶子自身、含根），叶子在前。"""
        chain: list[Node] = []
        cur: Node | None = leaf
        while cur is not None:
            chain.append(cur)
            cur = self.parent[cur.node_id]
        return chain

    def subtree_leaves(self, node: Node) -> Iterable[Node]:
        """枚举以 node 为根的子树中的叶子（DFS 顺序）。"""
        if node.is_leaf():
            yield node
        else:
            for child in node.children:
                yield from self.subtree_leaves(child)
