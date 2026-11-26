"""计划器：判定多个报表请求能否合成一次扫描，并产出共享计划与逐请求投影。

可共享的前提（任一不满足都会在 :class:`BlockingReasons` 中定位到具体请求与条件）：

1. 所有请求读取同一张受限关系（表），且引用的分区都存在；
2. 分组键集合完全一致（顺序按首个请求规范化）；键列的类型族必须一致；
3. 列引用、字面量类型、SUM/AVG 的数值性等局部约束合法；
4. HAVING 只引用分组键或本请求的聚合输出；
5. COUNT DISTINCT 跨多个分区时，(表, 去重列) 必须在目录中显式标示
   ``exclusive_distinct``（跨分区互斥），否则定位为
   ``DISTINCT_MERGE_NOT_EXCLUSIVE``——只扫一个分区时不发生跨分区合并，无需标示。

不会阻塞共享、而由投影阶段承载的差异：
* WHERE 谓词不同 -> 条件聚合，每个谓词 intern 成一个条件 id，相同谓词复用；
* 空输入策略（一行 NULL / 零行）与空组策略（DROP / 按域补齐）；
* HAVING 与聚合别名、输出列裁剪；
* AVG 与 SUM/COUNT 之间通过成对的 sum/count 状态互相重用。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .model import (
    Agg,
    AggFn,
    Catalog,
    Cmp,
    EmptyGroupPolicy,
    Predicate,
    Request,
    Semantics,
    type_family,
)
from .predicates import referenced_columns, walk_predicate
from .states import (
    COUNT,
    COUNT_STAR,
    DISTINCT_PARTIAL,
    DISTINCT_SET,
    MAX,
    MIN,
    SUM,
    StateSpec,
)

# 阻塞原因代码
TABLE_MISMATCH = "TABLE_MISMATCH"
UNKNOWN_TABLE = "UNKNOWN_TABLE"
UNKNOWN_PARTITION = "UNKNOWN_PARTITION"
GROUP_KEY_MISMATCH = "GROUP_KEY_MISMATCH"
UNKNOWN_COLUMN = "UNKNOWN_COLUMN"
TYPE_MISMATCH = "TYPE_MISMATCH"
NON_NUMERIC_AGG = "NON_NUMERIC_AGG"
DISTINCT_MERGE_NOT_EXCLUSIVE = "DISTINCT_MERGE_NOT_EXCLUSIVE"
BAD_HAVING_REF = "BAD_HAVING_REF"
INVALID_LITERAL_TYPE = "INVALID_LITERAL_TYPE"
DUPLICATE_ALIAS = "DUPLICATE_ALIAS"
DOMAIN_ARITY = "DOMAIN_ARITY"
AGG_EMPTY = "AGG_EMPTY"
NAME_DUPLICATE = "NAME_DUPLICATE"


@dataclass(frozen=True)
class Reason:
    request: str
    code: str
    detail: str


class BlockingReasons(Exception):
    """一个或多个请求无法纳入共享计划，附全部定位信息。"""

    def __init__(self, reasons: list[Reason]):
        self.reasons = reasons
        msg = "共享计划被阻塞：\n" + "\n".join(
            f"  - [{r.code}] 请求 {r.request!r}: {r.detail}" for r in reasons
        )
        super().__init__(msg)


# --------------------------------------------------------------------------- #
# 计划产物
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Binding:
    """聚合别名到共享状态的投影绑定。"""

    alias: str
    kind: str  # "state" | "avg"
    state_key: Optional[tuple] = None
    sum_key: Optional[tuple] = None
    count_key: Optional[tuple] = None
    include_null: bool = True


@dataclass(frozen=True)
class Projection:
    """一个原始请求在共享计划上的投影定义。"""

    request_name: str
    partitions: frozenset[str]
    group_keys: tuple[str, ...]
    aggs: tuple[Agg, ...]
    cond_id: Optional[str]
    having: Optional[Predicate]
    semantics: Semantics
    key_domain: tuple[tuple, ...]
    bindings: dict[str, Binding]
    presence_key: tuple


@dataclass(frozen=True)
class SharedPlan:
    table: str
    partitions: tuple[str, ...]                 # 联合扫描的分区（稳定次序）
    group_keys: tuple[str, ...]
    conditions: dict[str, Predicate]            # 条件 id -> WHERE 谓词
    state_specs: dict[tuple, StateSpec]         # 状态键 -> 状态规格
    projections: dict[str, Projection]

    def describe(self) -> str:
        """生成人类可读的计划摘要。"""
        lines = [
            f"共享扫描: 表 {self.table} 分区 {list(self.partitions)}",
            f"分组键: {list(self.group_keys)}",
            f"条件数: {len(self.conditions)}",
            f"共享状态数: {len(self.state_specs)}",
        ]
        for key, spec in sorted(self.state_specs.items(), key=lambda kv: repr(kv[0])):
            lines.append(f"  状态 {key} -> {spec}")
        for name, proj in self.projections.items():
            lines.append(
                f"  投影 {name}: cond={proj.cond_id} 绑定="
                + ", ".join(f"{a}->{b.kind}" for a, b in proj.bindings.items())
            )
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 计划器实现
# --------------------------------------------------------------------------- #
_NUMERIC = {"int", "float"}


def _families_compatible(a: Optional[str], b: Optional[str]) -> bool:
    if a is None or b is None:
        return True
    if a == b:
        return True
    return {a, b} <= _NUMERIC


def _agg_output_family(agg: Agg, schema: dict[str, str]) -> Optional[str]:
    if agg.fn in (AggFn.COUNT_STAR, AggFn.COUNT, AggFn.COUNT_DISTINCT):
        return "int"
    if agg.fn == AggFn.AVG:
        return "float"
    if agg.column is None:
        return None
    return schema.get(agg.column)


def _validate_predicate_types(
    req_name: str,
    pred: Optional[Predicate],
    schema: dict[str, str],
    reasons: list[Reason],
    having_outputs: Optional[dict[str, Optional[str]]] = None,
) -> None:
    """校验 Cmp 谓词的字面量与列类型族是否兼容。

    列存在性由调用方统一收集；NULL 字面量使比较恒为 UNKNOWN，不阻塞。
    ``having_outputs`` 为 None 时按 WHERE 查表列，否则按 HAVING 查输出列。
    """
    for node in walk_predicate(pred):
        if not isinstance(node, Cmp):
            continue
        lit_family = type_family(node.literal)
        if lit_family is None:
            continue
        if having_outputs is None:
            expected = schema.get(node.column)
        else:
            expected = having_outputs.get(node.column)
        if expected is not None and not _families_compatible(expected, lit_family):
            reasons.append(
                Reason(
                    req_name,
                    INVALID_LITERAL_TYPE,
                    f"列 {node.column!r} 类型族 {expected} 与字面量 "
                    f"{node.literal!r}({lit_family}) 不兼容",
                )
            )


def build_shared_plan(requests: list[Request], catalog: Catalog) -> SharedPlan:
    """把多个请求改写为一次扫描的共享计划；不可共享时抛出 :class:`BlockingReasons`。"""
    reasons: list[Reason] = []

    if not requests:
        raise BlockingReasons([Reason("<plan>", AGG_EMPTY, "请求列表为空")])

    seen_names: set[str] = set()
    for req in requests:
        if req.name in seen_names:
            reasons.append(Reason(req.name, NAME_DUPLICATE, "请求名重复"))
        seen_names.add(req.name)

    base = requests[0]
    if base.table not in catalog.tables:
        reasons.append(Reason(base.name, UNKNOWN_TABLE, f"目录中不存在表 {base.table!r}"))
        raise BlockingReasons(reasons)

    base_table = catalog.tables[base.table]
    base_schema = base_table.schema
    canonical_keys = base.group_keys

    # 表与分区、分组键集合 -------------------------------------------------- #
    for req in requests:
        if req.table not in catalog.tables:
            reasons.append(Reason(req.name, UNKNOWN_TABLE, f"目录中不存在表 {req.table!r}"))
            continue
        if req.table != base.table:
            reasons.append(
                Reason(
                    req.name,
                    TABLE_MISMATCH,
                    f"请求读取 {req.table!r}，与基准表 {base.table!r} 不同，无法合并扫描",
                )
            )
        table = catalog.tables[req.table]
        for part in req.partitions:
            if part not in table.partitions:
                reasons.append(
                    Reason(req.name, UNKNOWN_PARTITION,
                           f"表 {req.table!r} 不存在分区 {part!r}")
                )
        if set(req.group_keys) != set(canonical_keys):
            reasons.append(
                Reason(
                    req.name,
                    GROUP_KEY_MISMATCH,
                    f"分组键 {list(req.group_keys)} 与基准 {list(canonical_keys)} 集合不一致"
                    "（本计划器不做上卷 ROLLUP 推导）",
                )
            )
        else:
            for key in req.group_keys:
                schema = catalog.tables[req.table].schema
                if key not in schema:
                    reasons.append(Reason(req.name, UNKNOWN_COLUMN, f"未知分组键 {key!r}"))
                elif key in base_schema and schema[key] != base_schema[key]:
                    reasons.append(
                        Reason(
                            req.name,
                            TYPE_MISMATCH,
                            f"分组键 {key!r} 类型族 {schema[key]} 与基准 {base_schema[key]} 不一致",
                        )
                    )
        if not req.aggs:
            reasons.append(Reason(req.name, AGG_EMPTY, "聚合集合为空"))
        aliases = [a.alias for a in req.aggs]
        if len(set(aliases)) != len(aliases):
            reasons.append(Reason(req.name, DUPLICATE_ALIAS, f"聚合别名重复: {aliases}"))

    # 局部列/类型/谓词校验 -------------------------------------------------- #
    for req in requests:
        if req.table not in catalog.tables:
            continue
        schema = catalog.tables[req.table].schema
        for agg in req.aggs:
            if agg.fn is AggFn.COUNT_STAR:
                continue
            if agg.column not in schema:
                reasons.append(
                    Reason(req.name, UNKNOWN_COLUMN,
                           f"聚合 {agg.alias} 引用未知列 {agg.column!r}")
                )
                continue
            family = schema[agg.column]
            if agg.fn in (AggFn.SUM, AggFn.AVG) and family not in _NUMERIC:
                reasons.append(
                    Reason(
                        req.name,
                        NON_NUMERIC_AGG,
                        f"{agg.fn.value} 要求数值列，{agg.column!r} 的类型族为 {family}",
                    )
                )
        for col in referenced_columns(req.where) - set(schema):
            reasons.append(Reason(req.name, UNKNOWN_COLUMN, f"WHERE 引用未知列 {col!r}"))
        _validate_predicate_types(req.name, req.where, schema, reasons)

        output_families: dict[str, Optional[str]] = {k: schema.get(k) for k in req.group_keys}
        for agg in req.aggs:
            output_families[agg.alias] = _agg_output_family(agg, schema)
        for col in referenced_columns(req.having) - set(output_families):
            reasons.append(
                Reason(req.name, BAD_HAVING_REF,
                       f"HAVING 引用了非分组键/非聚合输出 {col!r}")
            )
        _validate_predicate_types(
            req.name, req.having, schema, reasons, having_outputs=output_families
        )

        if req.semantics.empty_group is EmptyGroupPolicy.KEEP_DOMAIN:
            for tup in req.key_domain:
                if len(tup) != len(req.group_keys):
                    reasons.append(
                        Reason(
                            req.name,
                            DOMAIN_ARITY,
                            f"key_domain 元组 {tup!r} 元数与分组键数 {len(req.group_keys)} 不符",
                        )
                    )
                    break

    # COUNT DISTINCT 跨分区互斥检查 ---------------------------------------- #
    for req in requests:
        if req.table not in catalog.tables:
            continue
        for agg in req.aggs:
            if agg.fn is not AggFn.COUNT_DISTINCT:
                continue
            if (
                len(req.partitions) > 1
                and (req.table, agg.column) not in catalog.exclusive_distinct
            ):
                reasons.append(
                    Reason(
                        req.name,
                        DISTINCT_MERGE_NOT_EXCLUSIVE,
                        f"去重计数 {agg.alias} 跨 {len(req.partitions)} 个分区，"
                        f"但 (表={req.table}, 列={agg.column}) 未在目录标示跨分区互斥；"
                        "部分计数直接相加会重复计数",
                    )
                )

    if reasons:
        raise BlockingReasons(reasons)

    # ------------------------------------------------------------------ #
    # 构建共享状态与条件
    # ------------------------------------------------------------------ #
    conditions: dict[Predicate, str] = {}
    conditions_public: dict[str, Predicate] = {}

    def intern_condition(pred: Optional[Predicate]) -> Optional[str]:
        if pred is None:
            return None
        if pred not in conditions:
            cid = f"c{len(conditions)}"
            conditions[pred] = cid
            conditions_public[cid] = pred
        return conditions[pred]

    state_specs: dict[tuple, StateSpec] = {}

    def register(kind: str, column: Optional[str], cond_id: Optional[str],
                 scope: frozenset[str], include_null: bool = True,
                 exclusive: bool = False) -> tuple:
        # 状态键含分区范围：谓词相同但分区子集不同的请求必须各持一份状态，
        # 否则会把不属于某请求的分区行计入它的聚合。
        key = (kind, column, cond_id, include_null, scope)
        if key not in state_specs:
            state_specs[key] = StateSpec(
                kind=kind, include_null=include_null, exclusive=exclusive
            )
        return key

    union_partitions: list[str] = []
    for req in requests:
        for part in req.partitions:
            if part not in union_partitions:
                union_partitions.append(part)

    projections: dict[str, Projection] = {}

    for req in requests:
        cond_id = intern_condition(req.where)
        scope = frozenset(req.partitions)
        # 存在性状态：只统计本请求分区子集内且通过其 WHERE 的行
        presence_key = register(COUNT_STAR, None, cond_id, scope)

        bindings: dict[str, Binding] = {}
        for agg in req.aggs:
            col = agg.column
            if agg.fn is AggFn.COUNT_STAR:
                key = register(COUNT_STAR, None, cond_id, scope)
                bindings[agg.alias] = Binding(agg.alias, "state", state_key=key)
            elif agg.fn is AggFn.COUNT:
                key = register(COUNT, col, cond_id, scope)
                bindings[agg.alias] = Binding(agg.alias, "state", state_key=key)
            elif agg.fn is AggFn.SUM:
                key = register(SUM, col, cond_id, scope)
                bindings[agg.alias] = Binding(agg.alias, "state", state_key=key)
            elif agg.fn is AggFn.MIN:
                key = register(MIN, col, cond_id, scope)
                bindings[agg.alias] = Binding(agg.alias, "state", state_key=key)
            elif agg.fn is AggFn.MAX:
                key = register(MAX, col, cond_id, scope)
                bindings[agg.alias] = Binding(agg.alias, "state", state_key=key)
            elif agg.fn is AggFn.AVG:
                # 成对状态重用：AVG = SUM / COUNT(非空)
                sum_key = register(SUM, col, cond_id, scope)
                cnt_key = register(COUNT, col, cond_id, scope)
                bindings[agg.alias] = Binding(
                    agg.alias, "avg", sum_key=sum_key, count_key=cnt_key
                )
            elif agg.fn is AggFn.COUNT_DISTINCT:
                if len(req.partitions) <= 1:
                    # 单分区：保留值集合，不存在跨分区合并
                    key = register(DISTINCT_SET, col, cond_id, scope,
                                   include_null=agg.include_null)
                else:
                    # 多分区：计划器已确认互斥，使用可相加的部分计数状态
                    key = register(
                        DISTINCT_PARTIAL,
                        col,
                        cond_id,
                        scope,
                        include_null=agg.include_null,
                        exclusive=True,
                    )
                bindings[agg.alias] = Binding(
                    agg.alias, "state", state_key=key, include_null=agg.include_null
                )
            else:  # pragma: no cover
                raise AssertionError(f"未覆盖的聚合函数: {agg.fn}")

        projections[req.name] = Projection(
            request_name=req.name,
            partitions=frozenset(req.partitions),
            group_keys=tuple(canonical_keys),
            aggs=tuple(req.aggs),
            cond_id=cond_id,
            having=req.having,
            semantics=req.semantics,
            key_domain=tuple(req.key_domain),
            bindings=bindings,
            presence_key=presence_key,
        )

    return SharedPlan(
        table=base.table,
        partitions=tuple(union_partitions),
        group_keys=tuple(canonical_keys),
        conditions=conditions_public,
        state_specs=state_specs,
        projections=projections,
    )
