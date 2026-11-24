"""相关标量子查询的左连接改写（去相关）。

触发条件（缺一不可，保守策略）：
  1. 出现在投影列表顶层的标量子查询（输出单列）；
  2. 子查询 WHERE 中存在“内列 = 外列”形式的相关等值；
  3. 等值谓词中的内键（列集合）能被子查询作用域的元数据唯一键证明；
  4. 相关引用全部指向直接外层查询块（跨两层的相关不做改写）。

改写后：
  * 子查询体被提升为派生表，原相关等值成为 LEFT JOIN ... ON 的连接条件；
  * 其他过滤留在派生表内部，其余谓词放入 ON；
  * 原投影中的子查询表达式替换为派生表输出列；
  * 零匹配由左连接自然产生 NULL。

实现上先在“绑定树”上完成分析（读取 ResolvedColumn / BindReport），
再用 to_raw 产出未绑定树，最后重新走一次 bind 得到可执行的绑定树。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .binder import Binder
from .catalog import Catalog
from .errors import BinderError
from .model import (
    ColumnRef,
    DerivedTable,
    FuncCall,
    Join,
    Literal,
    SelectNode,
    SubqueryExpr,
    TableRef,
    and_,
)


@dataclass
class RewriteOutcome:
    rewritten: SelectNode
    bound_before: SelectNode
    bound_after: SelectNode
    applied: int = 0
    skipped: list[str] = field(default_factory=list)
    join_aliases: list[str] = field(default_factory=list)


def _conjuncts(expr):
    if expr is None:
        return []
    out = []
    stack = [expr]
    while stack:
        e = stack.pop()
        if isinstance(e, FuncCall) and e.name == "and":
            stack.extend(e.args)
        else:
            out.append(e)
    return out


def to_raw(expr):
    """把已绑定表达式转回未绑定树（重新绑定时按新作用域解析）。"""
    if isinstance(expr, ColumnRef):
        r = expr.resolved
        return ColumnRef(table=r.qualifier, name=r.name, alias=expr.alias,
                         location=expr.location)
    if isinstance(expr, FuncCall):
        return FuncCall(expr.name, [to_raw(a) for a in expr.args],
                        alias=expr.alias)
    if isinstance(expr, Literal):
        return Literal(value=expr.value, alias=expr.alias)
    if isinstance(expr, SubqueryExpr):
        raise BinderError("nested scalar subquery inside predicate is not rewritable")
    raise BinderError(f"cannot convert expression to raw: {type(expr)!r}")


def to_raw_rel(rel):
    # 提升阶段产出的节点已经是未绑定树，原样返回
    if getattr(rel, "rewrite_emitted", False):
        return rel
    if isinstance(rel, TableRef):
        return TableRef(name=rel.name, alias=rel.alias)
    if isinstance(rel, DerivedTable):
        return DerivedTable(query=to_raw_select(rel.query), alias=rel.alias,
                            lateral=rel.lateral)
    if isinstance(rel, Join):
        return Join(
            join_type=rel.join_type,
            left=to_raw_rel(rel.left),
            right=to_raw_rel(rel.right),
            on=to_raw(rel.on) if rel.on is not None else None,
            using=list(rel.using) if rel.using is not None else None,
            lateral=rel.lateral, alias=rel.alias,
        )
    raise BinderError(f"cannot convert relation to raw: {type(rel)!r}")


def to_raw_select(select: SelectNode) -> SelectNode:
    return SelectNode(
        projections=[_raw_projection(p) for p in select.projections],
        from_=to_raw_rel(select.from_) if select.from_ is not None else None,
        where=to_raw(select.where) if select.where is not None else None,
    )


def _raw_projection(p):
    if isinstance(p, SubqueryExpr):
        return SubqueryExpr(query=to_raw_select(p.query), alias=p.alias)
    return to_raw(p)


def _eq(a, b):
    return FuncCall("=", [a, b])


def _scope_depth(select: SelectNode):
    for rc in select.report.scope.ordered:
        return rc.depth
    return 0


class _Rewriter:
    def __init__(self, catalog: Catalog):
        self.catalog = catalog
        self.applied = 0
        self.skipped: list[str] = []
        self.join_aliases: list[str] = []
        self._alias_seq = 0
        self._used_aliases: set[str] = set()

    def _new_alias(self, scope=None):
        # 避让用户已使用的表别名，保证生成名在作用域内唯一
        if scope is not None:
            for tbl in scope.tables:
                self._used_aliases.add(tbl.alias)
        while True:
            self._alias_seq += 1
            candidate = f"__dq_{self._alias_seq}"
            if candidate not in self._used_aliases:
                self._used_aliases.add(candidate)
                return candidate

    # -- 入口：在绑定树上递归，产出未绑定树 -------------------------------
    def process(self, select: SelectNode) -> SelectNode:
        # 先处理投影中的标量子查询：提升动作会把新 LEFT JOIN 挂到 select.from_
        raw_projections = [
            self._process_projection(p, select) for p in select.projections
        ]
        # 再序列化（已被改写的）FROM 与 WHERE
        raw_from = self._process_from(select.from_)
        raw_where = self._process_predicate(select.where)
        return SelectNode(projections=raw_projections, from_=raw_from,
                          where=raw_where)

    def _process_from(self, rel):
        if rel is None:
            return None
        # 提升阶段构造的 LEFT JOIN 已经是未绑定树，直接做结构拷贝
        if getattr(rel, "rewrite_emitted", False):
            return to_raw_rel(rel)
        if isinstance(rel, TableRef):
            return to_raw_rel(rel)
        if isinstance(rel, DerivedTable):
            return DerivedTable(query=self.process(rel.query), alias=rel.alias,
                                lateral=rel.lateral)
        if isinstance(rel, Join):
            return Join(
                join_type=rel.join_type,
                left=self._process_from(rel.left),
                right=self._process_from(rel.right),
                on=self._process_predicate(rel.on),
                using=list(rel.using) if rel.using is not None else None,
                lateral=rel.lateral, alias=rel.alias,
            )
        raise BinderError(f"unsupported relation: {type(rel)!r}")

    def _process_predicate(self, expr):
        if expr is None:
            return None

        def walk(e):
            if isinstance(e, SubqueryExpr):
                # WHERE 中的标量子查询：仅递归改写其内部，保持子查询形态
                raw_sq = SubqueryExpr(query=self.process(e.query), alias=e.alias)
                return raw_sq
            if isinstance(e, FuncCall):
                return FuncCall(e.name, [walk(a) for a in e.args], alias=e.alias)
            return to_raw(e)

        return walk(expr)

    def _process_projection(self, proj, outer: SelectNode):
        if isinstance(proj, SubqueryExpr):
            replacement = self._try_lift(proj, outer)
            if replacement is not None:
                return replacement
            self.skipped.append(
                "scalar subquery does not meet unique-key decorrelation conditions"
            )
            return SubqueryExpr(query=self.process(proj.query), alias=proj.alias)
        return self._process_predicate(proj)

    # -- 单个标量子查询提升 -------------------------------------------------
    def _try_lift(self, sq: SubqueryExpr, outer: SelectNode):
        inner = sq.query
        if not inner.is_scalar() or inner.from_ is None:
            return None
        inner_report = inner.report
        if not inner_report.free_vars:
            return None
        outer_depth = _scope_depth(outer)
        if any(fv.depth != outer_depth for fv in inner_report.free_vars):
            self.skipped.append("correlation crosses more than one query block")
            return None

        inner_scope = inner_report.scope
        inner_preds = _conjuncts(inner.where)
        outer_cols_used = {
            (fv.qualifier, fv.name) for fv in inner_report.free_vars
        }

        corr_pairs = []   # (inner ResolvedColumn, outer ResolvedColumn)
        inner_only = []
        residual = []
        for pred in inner_preds:
            pair = self._as_correlation_eq(pred, outer_depth)
            if pair is None:
                if self._only_inner(pred, outer_depth):
                    inner_only.append(pred)
                else:
                    residual.append(pred)
                continue
            corr_pairs.append(pair)

        if not corr_pairs:
            self.skipped.append("no inner=outer equality correlation found")
            return None

        inner_cols = [p[0] for p in corr_pairs]
        if not self._proves_unique(inner_cols, inner_scope):
            self.skipped.append(
                "inner correlation columns are not proven unique by metadata"
            )
            return None

        # 投影中不允许出现额外的相关引用（否则提升后语义改变）
        for proj in inner.projections:
            refs = _collect_refs(proj)
            if any(r.resolved.depth == outer_depth for r in refs):
                self.skipped.append("correlated references appear in subquery output")
                return None

        # 在外层作用域的表别名集合内避让
        alias = self._new_alias(outer.report.scope)
        value_out = inner.projections[0]
        value_name = value_out.alias or "v"
        key_names = [f"k{i + 1}" for i in range(len(corr_pairs))]

        derived_projections = []
        for (in_col, _), kname in zip(corr_pairs, key_names):
            derived_projections.append(
                ColumnRef(table=in_col.qualifier, name=in_col.name, alias=kname)
            )
        value_raw = to_raw(value_out)
        value_raw.alias = value_name
        derived_projections.append(value_raw)

        derived = SelectNode(
            projections=derived_projections,
            from_=to_raw_rel(inner.from_),
            where=and_(*[to_raw(p) for p in inner_only]),
        )
        derived_table = DerivedTable(query=derived, alias=alias, lateral=False)

        on_preds = []
        for (_, out_col), kname in zip(corr_pairs, key_names):
            on_preds.append(_eq(
                ColumnRef(table=alias, name=kname),
                ColumnRef(table=out_col.qualifier, name=out_col.name),
            ))
        for p in residual:
            on_preds.append(to_raw(p))

        # outer.from_ 已在 process() 中递归改写为未绑定树
        join = Join(
            join_type="left", left=self._process_from(outer.from_),
            right=derived_table, on=and_(*on_preds), lateral=False,
        )
        join.rewrite_emitted = True
        # 把连接挂到外层原始节点上，process() 返回时即采用新 FROM
        outer.from_ = join
        self.applied += 1
        self.join_aliases.append(alias)
        sq.decorrelated = True
        sq.join_alias = alias
        return ColumnRef(table=alias, name=value_name,
                         alias=sq.alias or value_name)

    @staticmethod
    def _as_correlation_eq(pred, outer_depth):
        if not (isinstance(pred, FuncCall) and pred.name == "="
                and len(pred.args) == 2):
            return None
        a, b = pred.args
        if not (isinstance(a, ColumnRef) and isinstance(b, ColumnRef)):
            return None
        ra, rb = a.resolved, b.resolved
        a_outer = ra.depth == outer_depth
        b_outer = rb.depth == outer_depth
        if a_outer and not b_outer:
            return rb, ra
        if b_outer and not a_outer:
            return ra, rb
        return None

    @staticmethod
    def _only_inner(pred, outer_depth):
        refs = _collect_refs(pred)
        return bool(refs) and all(r.resolved.depth != outer_depth for r in refs)

    @staticmethod
    def _proves_unique(inner_cols, inner_scope):
        # inner_cols 是 ResolvedColumn 列表；键的每一列由其 base_refs 血缘标识
        refs_used = set()
        for c in inner_cols:
            refs_used.update(c.base_refs)
        for key in inner_scope.unique_keys:
            need = {part for key_col in key for part in key_col}
            if need and need <= refs_used:
                return True
        for tbl in inner_scope.tables:
            for key in tbl.unique_keys:
                need = {part for key_col in key for part in key_col}
                if need and need <= refs_used:
                    return True
        return False


def _collect_refs(expr):
    out = []
    stack = [expr]
    while stack:
        e = stack.pop()
        if isinstance(e, ColumnRef):
            out.append(e)
        elif isinstance(e, FuncCall):
            stack.extend(e.args)
    return out


def rewrite_query(query: SelectNode, catalog: Catalog) -> RewriteOutcome:
    """绑定 -> 去相关改写 -> 重新绑定，返回改写前后的树与统计。"""
    bound_before = Binder(catalog).bind_root(query)
    rewriter = _Rewriter(catalog)
    raw_rewritten = rewriter.process(bound_before)
    bound_after = Binder(catalog).bind_root(raw_rewritten)
    return RewriteOutcome(
        rewritten=raw_rewritten,
        bound_before=bound_before,
        bound_after=bound_after,
        applied=rewriter.applied,
        skipped=list(rewriter.skipped),
        join_aliases=list(rewriter.join_aliases),
    )
