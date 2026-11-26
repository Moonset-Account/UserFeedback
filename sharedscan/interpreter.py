"""小型关系解释器。

提供两条执行路径，且共用 :mod:`sharedscan.states` 的同一套状态机：

* :func:`execute_request`：按请求原始定义直接执行（改写前的参考语义）；
* :func:`execute_shared_plan`：执行计划器产出的一次扫描共享计划，再按
  每个请求的 :class:`~sharedscan.planner.Projection` 投影出原始结果。

两条路径的逐行、逐类型对照由 :mod:`sharedscan.verify` 完成。

执行顺序（两条路径一致）
========================
拼分区 -> WHERE(3VL) -> 分组聚合（AVG 拆成 sum/count 成对状态）
-> 空输入/空组处理 -> HAVING。

空组语义
========
* 无分组键 + 零输入行：``ONE_NULL_ROW`` 输出一行空聚合值，``ZERO_ROWS`` 输出零行；
* 有分组键：``DROP`` 只保留实际出现的组；``KEEP_DOMAIN`` 用空聚合值补齐
  ``key_domain`` 中缺席的组（NULL 键本身是合法组，不与“缺席”混淆）；
* HAVING 在空组补齐**之后**求值，因此可以基于 0/NULL 行值过滤补齐行。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .model import (
    Agg,
    AggFn,
    Catalog,
    EmptyGroupPolicy,
    EmptyInputPolicy,
    Request,
    Row,
    Scalar,
)
from .planner import Binding, Projection, SharedPlan
from .predicates import row_passes
from .states import (
    COUNT,
    COUNT_STAR,
    DISTINCT_PARTIAL,
    DISTINCT_SET,
    MAX,
    MIN,
    SUM,
    StateSpec,
    avg_from_pair,
    empty_value,
    finalize_state,
    merge_states,
    new_state,
    update_state,
)


@dataclass
class RequestResult:
    """一次请求执行的结果。"""

    name: str
    columns: tuple[str, ...]
    rows: list[tuple[Scalar, ...]]
    #: 每列在实际输出中观察到的类型族集合（NULL 记为 ``None`` 族）。
    column_families: dict[str, set[Optional[str]]]


# --------------------------------------------------------------------------- #
# 通用辅助
# --------------------------------------------------------------------------- #
def _state_kind(agg: Agg) -> str:
    return {
        AggFn.COUNT_STAR: COUNT_STAR,
        AggFn.COUNT: COUNT,
        AggFn.SUM: SUM,
        AggFn.MIN: MIN,
        AggFn.MAX: MAX,
        AggFn.COUNT_DISTINCT: DISTINCT_SET,  # 参考路径始终保留集合，天然精确
    }[agg.fn]


def _agg_state_spec(agg: Agg) -> StateSpec:
    if agg.fn is AggFn.COUNT_DISTINCT:
        return StateSpec(DISTINCT_SET, include_null=agg.include_null)
    return StateSpec(_state_kind(agg))


def _family_of(value: Scalar, aggs: tuple[Agg, ...], column: str) -> Optional[str]:
    """输出值的类型族；AVG 的非空输出恒为 float，即使输入列是 int。"""
    if value is None:
        return None
    for agg in aggs:
        if agg.alias == column and agg.fn is AggFn.AVG:
            return "float"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    raise TypeError(f"不支持的输出值类型: {type(value)!r}")


def _record_families(columns: tuple[str, ...], aggs: tuple[Agg, ...],
                     rows: list[tuple], families: dict[str, set]) -> None:
    for tup in rows:
        for col, val in zip(columns, tup):
            families[col].add(_family_of(val, aggs, col))


# --------------------------------------------------------------------------- #
# 参考执行（改写前）：AVG 同样由 sum/count 成对状态实现，与共享计划同构
# --------------------------------------------------------------------------- #
def _bind_reference_states(request: Request) -> dict[str, object]:
    slots: dict[str, object] = {}
    for agg in request.aggs:
        if agg.fn is AggFn.AVG:
            slots[f"{agg.alias}#sum"] = new_state(StateSpec(SUM))
            slots[f"{agg.alias}#cnt"] = new_state(StateSpec(COUNT))
        else:
            slots[agg.alias] = new_state(_agg_state_spec(agg))
    return slots


def _update_reference(request: Request, slots: dict[str, object], row: Row) -> None:
    for agg in request.aggs:
        if agg.fn is AggFn.COUNT_STAR:
            update_state(slots[agg.alias], None)
        elif agg.fn is AggFn.AVG:
            update_state(slots[f"{agg.alias}#sum"], row.get(agg.column))
            update_state(slots[f"{agg.alias}#cnt"], row.get(agg.column))
        else:
            update_state(slots[agg.alias], row.get(agg.column))


def _project_reference(request: Request, slots: dict[str, object]) -> tuple[Scalar, ...]:
    out: list[Scalar] = []
    for agg in request.aggs:
        if agg.fn is AggFn.AVG:
            s = finalize_state(StateSpec(SUM), slots[f"{agg.alias}#sum"])
            c = finalize_state(StateSpec(COUNT), slots[f"{agg.alias}#cnt"])
            out.append(avg_from_pair(s, c))
        else:
            out.append(finalize_state(_agg_state_spec(agg), slots[agg.alias]))
    return tuple(out)


def _empty_reference_row(request: Request) -> tuple[Scalar, ...]:
    out: list[Scalar] = []
    for agg in request.aggs:
        out.append(None if agg.fn is AggFn.AVG else empty_value(_state_kind(agg)))
    return tuple(out)


def execute_request(request: Request, catalog: Catalog) -> RequestResult:
    """按原始请求执行：拼分区 -> WHERE -> 分组聚合 -> 空组 -> HAVING。"""
    table = catalog.tables[request.table]
    rows: list[Row] = []
    for part in request.partitions:
        rows.extend(table.partitions[part])
    filtered = [r for r in rows if row_passes(request.where, r)]

    columns = tuple(request.group_keys) + tuple(a.alias for a in request.aggs)
    families: dict[str, set[Optional[str]]] = {c: set() for c in columns}
    out_rows: list[tuple] = []

    if not request.group_keys:
        slots = _bind_reference_states(request)
        for row in filtered:
            _update_reference(request, slots, row)
        if not filtered:
            if request.semantics.empty_input is EmptyInputPolicy.ONE_NULL_ROW:
                out_rows = [_empty_reference_row(request)]
        else:
            out_rows = [_project_reference(request, slots)]
    else:
        groups: dict[tuple, dict] = {}
        order: list[tuple] = []
        for row in filtered:
            key = tuple(row.get(k) for k in request.group_keys)
            if key not in groups:
                groups[key] = _bind_reference_states(request)
                order.append(key)
            _update_reference(request, groups[key], row)

        if request.semantics.empty_group is EmptyGroupPolicy.KEEP_DOMAIN:
            for domain_key in request.key_domain:
                if domain_key not in groups:
                    groups[domain_key] = _bind_reference_states(request)
                    order.append(domain_key)

        out_rows = [key + _project_reference(request, groups[key]) for key in order]

    if request.having is not None:
        out_rows = [
            tup for tup in out_rows
            if row_passes(request.having, dict(zip(columns, tup)))
        ]

    _record_families(columns, request.aggs, out_rows, families)
    return RequestResult(request.name, columns, out_rows, families)


# --------------------------------------------------------------------------- #
# 共享计划执行（改写后：一次联合扫描 + 逐请求投影）
# --------------------------------------------------------------------------- #
def execute_shared_plan(plan: SharedPlan, catalog: Catalog) -> dict[str, RequestResult]:
    """执行一次联合扫描，在扫描中维护全部共享状态，再逐请求投影。"""
    table = catalog.tables[plan.table]
    state_specs = plan.state_specs
    global_states: dict[tuple, dict[tuple, object]] = {}

    for part in plan.partitions:
        # 分区局部状态：DISTINCT_PARTIAL 在分区内先用去重集合累积
        local_states: dict[tuple, dict[tuple, object]] = {}
        for row in table.partitions[part]:
            group_key = (
                tuple(row.get(k) for k in plan.group_keys) if plan.group_keys else ()
            )
            bucket = local_states.setdefault(group_key, {})
            for state_key, spec in state_specs.items():
                kind, column, cond_id, _include_null, scope = state_key
                if part not in scope:
                    continue
                if cond_id is not None and not row_passes(plan.conditions[cond_id], row):
                    continue
                state = bucket.get(state_key)
                if state is None:
                    local_kind = (
                        DISTINCT_SET if kind == DISTINCT_PARTIAL else kind
                    )
                    state = new_state(
                        StateSpec(local_kind, spec.include_null, spec.exclusive)
                    )
                    bucket[state_key] = state
                value = None if kind == COUNT_STAR else row.get(column)
                update_state(state, value)

        # 分区边界：局部状态并入全局状态
        for group_key, bucket in local_states.items():
            global_bucket = global_states.setdefault(group_key, {})
            for state_key, local_state in bucket.items():
                spec = state_specs[state_key]
                if spec.kind == DISTINCT_PARTIAL:
                    # 计划器已校验互斥：分区内去重计数可直接相加
                    partial = new_state(
                        StateSpec(DISTINCT_PARTIAL, spec.include_null, exclusive=True)
                    )
                    partial.value = local_state.count()
                    if state_key in global_bucket:
                        partial.value += global_bucket[state_key].value
                    global_bucket[state_key] = partial
                elif state_key in global_bucket:
                    global_bucket[state_key] = merge_states(
                        spec, global_bucket[state_key], local_state
                    )
                else:
                    global_bucket[state_key] = local_state

    return {
        name: _project(name, proj, plan, global_states)
        for name, proj in plan.projections.items()
    }


def _binding_value(binding: Binding, states: dict[tuple, object],
                   plan: SharedPlan) -> Scalar:
    if binding.kind == "avg":
        s = finalize_state(plan.state_specs[binding.sum_key], states[binding.sum_key])
        c = finalize_state(plan.state_specs[binding.count_key],
                           states[binding.count_key])
        return avg_from_pair(s, c)
    spec = plan.state_specs[binding.state_key]
    return finalize_state(spec, states[binding.state_key])


def _empty_binding_value(binding: Binding, plan: SharedPlan) -> Scalar:
    if binding.kind == "avg":
        return None
    return empty_value(plan.state_specs[binding.state_key].kind)


def _project(name: str, proj: Projection, plan: SharedPlan,
             global_states: dict[tuple, dict]) -> RequestResult:
    keys = proj.group_keys
    columns = tuple(keys) + tuple(a.alias for a in proj.aggs)
    families: dict[str, set[Optional[str]]] = {c: set() for c in columns}
    out_rows: list[tuple] = []

    def build_row(group_key: tuple, states: Optional[dict]) -> tuple:
        values: list[Scalar] = []
        for agg in proj.aggs:
            binding = proj.bindings[agg.alias]
            if states is None:
                values.append(_empty_binding_value(binding, plan))
            else:
                values.append(_binding_value(binding, states, plan))
        return tuple(group_key) + tuple(values)

    if not keys:
        states = global_states.get(())
        presence = states.get(proj.presence_key) if states else None
        n_rows = presence.value if presence is not None else 0
        if n_rows == 0:
            if proj.semantics.empty_input is EmptyInputPolicy.ONE_NULL_ROW:
                out_rows = [build_row((), None)]
        else:
            out_rows = [build_row((), states)]
    else:
        seen: set[tuple] = set()
        for group_key, states in global_states.items():
            presence = states.get(proj.presence_key)
            if presence is None or presence.value == 0:
                continue  # 该组没有任何属于本请求（分区范围 + WHERE）的行
            seen.add(group_key)
            out_rows.append(build_row(group_key, states))
        if proj.semantics.empty_group is EmptyGroupPolicy.KEEP_DOMAIN:
            for domain_key in proj.key_domain:
                if domain_key not in seen:
                    out_rows.append(build_row(domain_key, None))

    if proj.having is not None:
        out_rows = [
            tup for tup in out_rows
            if row_passes(proj.having, dict(zip(columns, tup)))
        ]

    _record_families(columns, proj.aggs, out_rows, families)
    return RequestResult(name, columns, out_rows, families)
