"""分步装载规划器入口 :func:`plan_load`。"""

from __future__ import annotations

from .checker import ConstraintChecker
from .errors import SchemaError
from .graph import (
    DepEdge,
    Node,
    build_dependency_graph,
    find_unbreakable_cycle,
    topological_sort,
)
from .model import (
    DeferredFK,
    Insert,
    PlanResult,
    Row,
    TableSpec,
    Update,
)
from .solver import minimum_feedback_edge_set


def plan_load(
    tables: dict[str, TableSpec] | list[TableSpec],
    rows: list[Row],
    existing_keys: dict[str, set[tuple]] | None = None,
) -> PlanResult:
    """为一批记录生成分步装载计划。

    :param tables: 表结构（按表名映射，或表结构列表）。
    :param rows: 本批待装载的记录。
    :param existing_keys: 库中已存在的目标键，``{表名: {主键元组, ...}}``。
    :returns: 可解时给出按序排列的 INSERT/UPDATE 语句；不可解时
        ``feasible=False`` 且 ``cycle`` 携带一条不可打断的依赖环。
    """
    table_map = _normalize_tables(tables)
    existing = {name: set(keys) for name, keys in (existing_keys or {}).items()}
    _validate_schema(table_map)

    nodes, edges = build_dependency_graph(table_map, rows, existing)

    # 1) 不可暂缓边自身成环 -> 无解，返回一条不可打断的环。
    hard_cycle = find_unbreakable_cycle(nodes, edges)
    if hard_cycle is not None:
        return PlanResult(
            feasible=False,
            cycle=hard_cycle,
            reason="环上的外键均不可暂缓（含非空列或未授权临时置空的列）",
        )

    # 2) 选总代价最低的暂缓集合（代价相同取边 id 字典序最小者）。
    solved = minimum_feedback_edge_set(nodes, edges)
    if solved is None:  # 理论上前面已拦截，这里做防御性处理。
        hard_cycle = find_unbreakable_cycle(nodes, edges)
        return PlanResult(
            feasible=False,
            cycle=hard_cycle or (),
            reason="不存在能打断所有依赖环的暂缓集合",
        )
    removed_ids, total_cost = solved
    removed_set = set(removed_ids)
    removed_edges = [e for e in edges if e.edge_id in removed_set]
    active_edges = [e for e in edges if e.edge_id not in removed_set]

    # 3) 对剩余依赖做拓扑排序，确定插入顺序。
    order = topological_sort(nodes, active_edges)

    row_by_node: dict[Node, Row] = {
        (r.table, r.key(table_map[r.table].key_columns)): r for r in rows
    }
    deferred_by_source: dict[Node, list[DepEdge]] = {}
    for e in removed_edges:
        deferred_by_source.setdefault(e.source, []).append(e)
    position = {n: i for i, n in enumerate(nodes)}

    checker = ConstraintChecker(table_map, existing)
    inserts: list[Insert] = []
    updates: list[Update] = []
    statements: list[Insert | Update] = []
    deferred_records: list[DeferredFK] = []

    # 4a) 按拓扑序完成所有 INSERT：被暂缓的外键整组置空，其余外键在插入
    #     时必须已满足。每条 INSERT 都立即过一次约束检查。
    for node in order:
        row = row_by_node[node]
        values = dict(row.values)
        deferred_here = sorted(
            deferred_by_source.get(node, ()), key=lambda e: e.edge_id
        )
        defer_objs: list[DeferredFK] = []
        for e in deferred_here:
            for col in e.fk.columns:
                values[col] = None  # 临时置空（整组）。
            defer_objs.append(
                DeferredFK(
                    table=node[0],
                    row_key=node[1],
                    fk_name=e.fk.name,
                    columns=e.fk.columns,
                    cost=e.cost,
                )
            )
        insert = Insert(
            table=node[0],
            values=values,
            key=node[1],
            deferred=tuple(defer_objs),
        )
        checker.apply_insert(insert)
        inserts.append(insert)
        statements.append(insert)
        deferred_records.extend(defer_objs)

    # 4b) ASAP 补写：每轮选源行位置最靠前、且源/目标均已装载的暂缓边；
    #     每条 UPDATE 同样立即过约束检查。
    pending = set(removed_edges)
    while pending:
        ready = [
            e
            for e in pending
            if e.source in checker.loaded and e.target in checker.loaded
        ]
        if not ready:
            # 理论上不可达：所有行都已插入，目标必然存在。
            raise RuntimeError("内部错误：存在无法补写的暂缓外键")
        ready.sort(key=lambda e: (position[e.source], e.edge_id))
        e = ready[0]
        assignments = dict(zip(e.fk.columns, e.fk_value))
        upd = Update(
            table=e.source[0],
            key=e.source[1],
            fk_name=e.fk.name,
            assignments=assignments,
        )
        checker.apply_update(upd)
        updates.append(upd)
        statements.append(upd)
        pending.remove(e)

    # 5) 终态检查：所有暂缓外键必须已补写。
    checker.assert_all_closed()

    return PlanResult(
        feasible=True,
        statements=tuple(statements),
        inserts=tuple(inserts),
        updates=tuple(updates),
        deferred=tuple(deferred_records),
        total_cost=total_cost,
    )


# ----------------------------------------------------------------------
def _normalize_tables(
    tables: dict[str, TableSpec] | list[TableSpec],
) -> dict[str, TableSpec]:
    if isinstance(tables, dict):
        return dict(tables)
    return {t.name: t for t in tables}


def _validate_schema(tables: dict[str, TableSpec]) -> None:
    for name, spec in tables.items():
        if spec.name != name:
            raise SchemaError(
                f"表名字典键 {name!r} 与 TableSpec.name {spec.name!r} 不一致"
            )
        col_names = [c.name for c in spec.columns]
        if len(set(col_names)) != len(col_names):
            raise SchemaError(f"表 {name} 存在重复列名")
        col_set = set(col_names)
        for col in spec.columns:
            if col.defer_cost is not None and not col.nullable:
                raise SchemaError(
                    f"非空列 {name}.{col.name} 不允许声明临时置空代价"
                )
            if col.defer_cost is not None and col.defer_cost < 0:
                raise SchemaError(
                    f"列 {name}.{col.name} 的置空代价不能为负"
                )
        for kc in spec.key_columns:
            if kc not in col_set:
                raise SchemaError(f"表 {name} 的主键列 {kc!r} 未在列定义中")
            col = spec.column_map[kc]
            if not col.nullable:
                pass  # 主键通常非空
            else:
                # 可空主键在关系模型里没有意义，这里直接拒绝。
                raise SchemaError(f"表 {name} 的主键列 {kc!r} 不能允许 NULL")
        fk_names = [fk.name for fk in spec.foreign_keys]
        if len(set(fk_names)) != len(fk_names):
            raise SchemaError(f"表 {name} 存在重复外键名")
        for fk in spec.foreign_keys:
            if fk.referenced_table not in tables:
                raise SchemaError(
                    f"表 {name} 的外键 {fk.name!r} 引用未知表 "
                    f"{fk.referenced_table!r}"
                )
            target = tables[fk.referenced_table]
            for c in fk.columns:
                if c not in col_set:
                    raise SchemaError(
                        f"表 {name} 的外键 {fk.name!r} 含未知列 {c!r}"
                    )
            if len(fk.referenced_columns) != len(fk.columns):
                raise SchemaError(f"外键 {fk.name!r} 列数不匹配")
            for rc in fk.referenced_columns:
                if rc not in target.column_map:
                    raise SchemaError(
                        f"外键 {fk.name!r} 的被引用列 {rc!r} 在目标表不存在"
                    )
