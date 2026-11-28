"""行级依赖图的构建、环检测与拓扑排序。

图节点是一行记录 ``(表名, 主键元组)``；一条有向边
``source -> target`` 表示 source 的某个外键引用 target，target 必须先
装载。库中已存在的目标键不产生边。
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import DanglingReferenceError, SchemaError
from .model import CycleEdge, ForeignKeySpec, Row, TableSpec

Node = tuple[str, tuple]


@dataclass(frozen=True)
class DepEdge:
    """一条行级外键依赖边。

    ``breakable`` 为 True 表示该外键允许整组临时置空；复合外键只有在
    其所有列都允许置空时才可拆开（这里的“拆开”指整组暂缓）。
    """

    edge_id: int
    source: Node
    target: Node
    fk: ForeignKeySpec
    breakable: bool
    cost: float
    fk_value: tuple


def build_dependency_graph(
    tables: dict[str, TableSpec],
    rows: list[Row],
    existing_keys: dict[str, set[tuple]],
) -> tuple[list[Node], list[DepEdge]]:
    """根据输入记录构建依赖图。

    :raises SchemaError: 表结构 / 外键声明不合法。
    :raises DanglingReferenceError: 外键目标在批内与库中都不存在。
    """
    nodes: list[Node] = []
    node_index: dict[Node, int] = {}
    in_batch: dict[str, set[tuple]] = {name: set() for name in tables}

    for row in rows:
        spec = tables.get(row.table)
        if spec is None:
            raise SchemaError(f"未知的表 {row.table!r}")
        key = row.key(spec.key_columns)
        node = (row.table, key)
        if node in node_index:
            raise SchemaError(f"批内出现重复主键 {row.table}{key}")
        node_index[node] = len(nodes)
        nodes.append(node)
        in_batch[row.table].add(key)

    edges: list[DepEdge] = []
    edge_id = 0
    for row in rows:
        spec = tables[row.table]
        node = (row.table, row.key(spec.key_columns))
        for fk in spec.foreign_keys:
            if fk.referenced_table not in tables:
                raise SchemaError(
                    f"外键 {fk.name!r} 引用了未知的表 {fk.referenced_table!r}"
                )
            present = [c in row.values for c in fk.columns]
            if not any(present):
                # 调用方未给出该外键的任何列值：视为天然暂缓，不产生依赖。
                continue
            if not all(present):
                raise SchemaError(
                    f"行 {node} 的复合外键 {fk.name!r} 只给了部分列，"
                    "复合外键必须整组提供或整组留空"
                )
            fk_value = tuple(row.values[c] for c in fk.columns)
            if any(v is None for v in fk_value):
                if not all(v is None for v in fk_value):
                    raise SchemaError(
                        f"行 {node} 的复合外键 {fk.name!r} 只置空了部分列，"
                        "复合外键只能整组置空"
                    )
                # 整组为 NULL，本身不构成引用。
                continue
            target_key = fk_value
            if target_key in existing_keys.get(fk.referenced_table, set()):
                continue  # 库中已存在，不产生批内依赖
            if target_key not in in_batch.get(fk.referenced_table, set()):
                raise DanglingReferenceError(
                    node, fk.name, fk.referenced_table, target_key
                )
            target = (fk.referenced_table, target_key)
            breakable, cost = _edge_breakability(spec, fk)
            edges.append(
                DepEdge(
                    edge_id=edge_id,
                    source=node,
                    target=target,
                    fk=fk,
                    breakable=breakable,
                    cost=cost,
                    fk_value=fk_value,
                )
            )
            edge_id += 1

    return nodes, edges


def _edge_breakability(
    spec: TableSpec, fk: ForeignKeySpec
) -> tuple[bool, float]:
    """判断一条外键边能否暂缓，并计算整组暂缓代价。"""
    col_map = spec.column_map
    cost = 0.0
    for name in fk.columns:
        col = col_map.get(name)
        if col is None:
            raise SchemaError(f"表 {spec.name} 缺少外键列 {name!r}")
        if not col.nullable:
            # 非空列必须在首次插入时满足，整条外键不可暂缓。
            return False, 0.0
        if col.defer_cost is None:
            return False, 0.0
        cost += col.defer_cost
    return True, cost


def find_unbreakable_cycle(
    nodes: list[Node], edges: list[DepEdge]
) -> tuple[CycleEdge, ...] | None:
    """在“仅保留不可暂缓边”的子图中找一条环。

    找到则返回环上的边序列（最后一条边指回环起点），无环返回 None。
    """
    adjacency: dict[Node, list[DepEdge]] = {n: [] for n in nodes}
    for e in edges:
        if not e.breakable:
            adjacency[e.source].append(e)

    color: dict[Node, int] = {n: 0 for n in nodes}  # 0 白 / 1 灰 / 2 黑
    node_stack: list[Node] = []
    edge_stack: list[DepEdge] = []

    def dfs(u: Node) -> list[DepEdge] | None:
        color[u] = 1
        node_stack.append(u)
        for e in adjacency[u]:
            v = e.target
            if color[v] == 0:
                edge_stack.append(e)
                found = dfs(v)
                if found is not None:
                    return found
                edge_stack.pop()
            elif color[v] == 1:
                # 找到回边：切出从 v 到当前节点的路径再加上回边。
                idx = node_stack.index(v)
                return edge_stack[idx:] + [e]
        color[u] = 2
        node_stack.pop()
        return None

    for n in nodes:
        if color[n] == 0:
            found = dfs(n)
            if found is not None:
                return tuple(
                    CycleEdge(
                        source_table=e.source[0],
                        source_key=e.source[1],
                        fk_name=e.fk.name,
                        target_table=e.target[0],
                        target_key=e.target[1],
                    )
                    for e in found
                )
    return None


def topological_sort(
    nodes: list[Node], active_edges: list[DepEdge]
) -> list[Node]:
    """生成满足依赖边的插入顺序（边 ``source -> target`` 表示 source
    依赖 target，target 必须先插入）。

    采用确定性 DFS 后序：按输入顺序遍历节点，沿依赖边深入，被依赖者
    先于依赖者输出；同一节点的多条出边按边 id 排序。存在环时抛
    ``ValueError``。
    """
    position = {n: i for i, n in enumerate(nodes)}
    adjacency: dict[Node, list[DepEdge]] = {n: [] for n in nodes}
    for e in active_edges:
        adjacency[e.source].append(e)
    for outs in adjacency.values():
        outs.sort(key=lambda e: e.edge_id)

    color: dict[Node, int] = {n: 0 for n in nodes}  # 0 白 / 1 灰 / 2 黑
    order: list[Node] = []

    def dfs(u: Node) -> None:
        color[u] = 1
        for e in adjacency[u]:
            v = e.target
            if color[v] == 0:
                dfs(v)
            elif color[v] == 1:
                raise ValueError("依赖图中仍存在环，无法拓扑排序")
        color[u] = 2
        order.append(u)

    for n in sorted(nodes, key=lambda x: position[x]):
        if color[n] == 0:
            dfs(n)
    return order
