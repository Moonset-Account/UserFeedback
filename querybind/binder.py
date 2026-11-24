"""名称绑定与自由列分析。

绑定产物：
  * ColumnRef.resolved -> ResolvedColumn（含限定名、绑定深度、位置、血缘、
    唯一性，以及 USING 合并列的 COALESCE 来源表达式）。
  * SelectNode.report  -> BindReport（自由列及其深度、输出描述）。

作用域链按查询块嵌套组织；LATERAL 左邻作用域被显式追加到链上，非横向
派生表看不到这些作用域，若名称只在左邻可见则抛 IllegalLateralError。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Optional

from .catalog import Catalog
from .errors import (
    AmbiguousColumnError,
    DuplicateAliasError,
    IllegalLateralError,
    UnresolvedColumnError,
)
from .model import (
    ColumnRef,
    DerivedTable,
    FuncCall,
    Join,
    Literal,
    SelectNode,
    Star,
    SubqueryExpr,
    TableRef,
)

# 基表血缘：(基表名, 基表列名)
BaseRef = tuple[str, str]


@dataclass
class ResolvedColumn:
    qualifier: Optional[str]
    name: str
    depth: int                       # 查询块嵌套深度（0 = 最外层）
    scope_position: int              # 在所在作用域星号序列中的位置
    table_scope_position: int        # 在所属表列序列中的位置
    base_refs: tuple[BaseRef, ...]
    unique: bool = False
    # USING 合并列的来源表达式（全外为 COALESCE(l, r)）
    source: Optional[object] = None
    is_using_merged: bool = False
    using_name: Optional[str] = None
    is_sibling: bool = False

    def key(self):
        return self.qualifier, self.name


@dataclass
class TableSym:
    alias: str
    columns: list[ResolvedColumn] = field(default_factory=list)
    # 每个唯一键是若干“血缘元组”的元组，与 ResolvedColumn.base_refs 对齐
    unique_keys: list[tuple[tuple[BaseRef, ...], ...]] = field(default_factory=list)
    kind: str = "base"


@dataclass
class Scope:
    tables: list[TableSym] = field(default_factory=list)
    ordered: list[ResolvedColumn] = field(default_factory=list)
    columns: dict[str, list[ResolvedColumn]] = field(default_factory=dict)
    unique_keys: list[tuple[tuple[BaseRef, ...], ...]] = field(default_factory=list)
    is_sibling: bool = False

    def find_table(self, alias):
        for t in self.tables:
            if t.alias == alias:
                return t
        return None


@dataclass
class FreeVar:
    name: str
    qualifier: Optional[str]
    depth: int
    sibling: bool
    location: Optional[str]
    base_refs: tuple[BaseRef, ...]


@dataclass
class OutputDescriptor:
    name: str
    qualifier: Optional[str]
    base_refs: tuple[BaseRef, ...]
    unique: bool
    position: int


@dataclass
class BindReport:
    nid: int
    free_vars: list[FreeVar] = field(default_factory=list)
    outputs: list[OutputDescriptor] = field(default_factory=list)
    scope: Optional[Scope] = None


# ---------------------------------------------------------------------------
class Binder:
    def __init__(self, catalog: Catalog):
        self.catalog = catalog
        self._next_nid = 1

    # -- 入口 --------------------------------------------------------------
    def bind_root(self, query: SelectNode) -> SelectNode:
        tree = copy.deepcopy(query)
        self._reset(tree)
        bound = self.bind_select(tree, outer_chain=[])
        return bound

    def _reset(self, node):
        stack = [node]
        while stack:
            cur = stack.pop()
            if isinstance(cur, SelectNode):
                cur.nid = None
                cur.report = None
                stack.extend(cur.projections)
                if cur.where is not None:
                    stack.append(cur.where)
                if cur.from_ is not None:
                    stack.append(cur.from_)
            elif isinstance(cur, SubqueryExpr):
                stack.append(cur.query)
                cur.decorrelated = False
                cur.join_alias = None
            elif isinstance(cur, Join):
                stack.append(cur.left)
                stack.append(cur.right)
                if cur.on is not None:
                    stack.append(cur.on)
                cur.bound_using = False
                cur.using_meta = None
                cur.rewrite_emitted = False
            elif isinstance(cur, DerivedTable):
                stack.append(cur.query)
            elif isinstance(cur, ColumnRef):
                cur.resolved = None

    # -- Select ------------------------------------------------------------
    def bind_select(self, select: SelectNode, outer_chain: list[Scope],
                    forbidden: Optional[list[Scope]] = None) -> SelectNode:
        nid = self._next_nid
        self._next_nid += 1
        select.nid = nid
        own_depth = len(outer_chain)
        report = BindReport(nid=nid)
        select.report = report

        if select.from_ is not None:
            scope, bound_from = self.bind_from(
                select.from_, depth=own_depth,
                outer_chain=outer_chain, sibling_scopes=[],
                forbidden=forbidden or [], report=report,
            )
            select.from_ = bound_from
        else:
            scope = Scope()

        chain = outer_chain + [scope]
        report.scope = scope

        # WHERE
        if select.where is not None:
            select.where = self.bind_expr(
                select.where, chain, own_depth, report, forbidden or []
            )

        # 投影：先展开星号（保持当前输出顺序），再绑定
        expanded: list = []
        for proj in select.projections:
            if isinstance(proj, Star):
                expanded.extend(self._expand_star(proj, scope))
            else:
                expanded.append(proj)
        bound_projections = []
        anon = 0
        for pos, proj in enumerate(expanded):
            bound = self.bind_expr(proj, chain, own_depth, report, forbidden or [])
            bound = self._name_output(bound, pos, anon)
            anon += 1
            bound_projections.append(bound)
            report.outputs.append(self._describe_output(bound, pos))
        select.projections = bound_projections
        return select

    def _name_output(self, expr, pos, anon):
        if getattr(expr, "alias", None):
            return expr
        if isinstance(expr, ColumnRef):
            expr.alias = expr.resolved.name
        elif isinstance(expr, SubqueryExpr):
            expr.alias = f"_col_{pos + 1}"
        else:
            expr.alias = f"_col_{pos + 1}"
        return expr

    def _describe_output(self, expr, pos):
        if isinstance(expr, ColumnRef):
            r = expr.resolved
            return OutputDescriptor(expr.alias, r.qualifier, r.base_refs, r.unique, pos)
        if isinstance(expr, SubqueryExpr):
            inner_out = expr.query.report.outputs[0]
            return OutputDescriptor(
                expr.alias, inner_out.qualifier, inner_out.base_refs, False, pos
            )
        if isinstance(expr, FuncCall):
            refs = self._collect_refs(expr)
            base = tuple(sorted({b for r in refs for b in r.resolved.base_refs}))
            return OutputDescriptor(expr.alias, None, base, False, pos)
        return OutputDescriptor(expr.alias, None, (), False, pos)

    # -- 星号 --------------------------------------------------------------
    def _expand_star(self, star: Star, scope: Scope):
        if star.table is None:
            cols = list(scope.ordered)
        else:
            tbl = scope.find_table(star.table)
            if tbl is None:
                raise UnresolvedColumnError(
                    f"unknown qualifier {star.table!r} for star expansion",
                    qualifier=star.table, column="*", location="select-list",
                )
            cols = list(tbl.columns)
        result = []
        for rc in cols:
            result.append(ColumnRef(
                table=rc.qualifier, name=rc.name, resolved=rc,
                location=f"star:{rc.qualifier}.{rc.name}",
            ))
        return result

    # -- FROM --------------------------------------------------------------
    def bind_from(self, rel, depth, outer_chain, sibling_scopes, forbidden,
                  report: BindReport):
        if isinstance(rel, TableRef):
            return self._bind_table(rel, depth), rel
        if isinstance(rel, DerivedTable):
            return self._bind_derived(rel, depth, outer_chain, sibling_scopes, forbidden)
        if isinstance(rel, Join):
            return self._bind_join(rel, depth, outer_chain, sibling_scopes,
                                   forbidden, report)
        raise TypeError(f"unsupported relation: {type(rel)!r}")

    def _bind_table(self, rel: TableRef, depth):
        data = self.catalog.get(rel.name)
        alias = rel.alias or rel.name
        cols = []
        for i, cname in enumerate(data.columns):
            cols.append(ResolvedColumn(
                qualifier=alias, name=cname, depth=depth,
                scope_position=i, table_scope_position=i,
                base_refs=((rel.name, cname),),
            ))
        keys = [
            tuple(((rel.name, c),) for c in key)
            for key in data.unique_keys
        ]
        for rc in cols:
            rc.unique = any(
                len(key) == 1 and key[0] == rc.base_refs for key in keys
            )
        sym = TableSym(alias=alias, columns=cols, unique_keys=keys, kind="base")
        return self._single_scope(sym, depth, keys)

    def _bind_derived(self, rel: DerivedTable, depth, outer_chain,
                      sibling_scopes, forbidden):
        if rel.lateral:
            # 左邻作用域对该派生表而言是“兄弟作用域”
            marked_siblings = [self._mark_sibling(s) for s in sibling_scopes]
            inner_chain = outer_chain + marked_siblings
            inner_forbidden = []
        else:
            inner_chain = outer_chain
            inner_forbidden = forbidden + sibling_scopes
        try:
            inner = self.bind_select(rel.query, inner_chain, inner_forbidden)
        except UnresolvedColumnError as exc:
            if not rel.lateral and self._visible_in(exc, inner_forbidden):
                raise IllegalLateralError(
                    f"non-lateral derived table {rel.alias!r} cannot reference "
                    f"left neighbour column {exc.qualifier + '.' if exc.qualifier else ''}"
                    f"{exc.column}",
                    qualifier=exc.qualifier, column=exc.column, location=exc.location,
                ) from exc
            raise
        rel.query = inner
        alias = rel.alias
        if alias is None:
            raise DuplicateAliasError("derived table requires an alias", location="from")

        cols = []
        for i, out in enumerate(inner.report.outputs):
            rc = ResolvedColumn(
                qualifier=alias, name=out.name, depth=depth,
                scope_position=i, table_scope_position=i,
                base_refs=out.base_refs, unique=out.unique,
                is_sibling=rel.lateral,
            )
            cols.append(rc)
        # 派生表唯一键：内层作用域键若被输出列原样暴露则向上传播
        keys = self._inherit_keys(inner.report.scope, cols)
        sym = TableSym(alias=alias, columns=cols, unique_keys=keys,
                       kind="derived-lateral" if rel.lateral else "derived")
        scope = self._single_scope(sym, depth, keys)
        scope.is_sibling = rel.lateral
        return scope, rel

    def _inherit_keys(self, inner_scope: Scope, out_cols):
        by_refs = {c.base_refs: c for c in out_cols}
        result = []
        for key in inner_scope.unique_keys:
            mapped = []
            ok = True
            for key_col_refs in key:
                out = by_refs.get(key_col_refs)
                if out is None or out.base_refs != key_col_refs:
                    ok = False
                    break
                mapped.append(key_col_refs)
            if ok:
                result.append(tuple(mapped))
        return result

    def _bind_join(self, rel: Join, depth, outer_chain, sibling_scopes,
                   forbidden, report: BindReport):
        left_scope, rel.left = self.bind_from(
            rel.left, depth, outer_chain, sibling_scopes, forbidden, report
        )
        right_siblings = sibling_scopes + [left_scope]
        right_scope, rel.right = self.bind_from(
            rel.right, depth, outer_chain, right_siblings, forbidden, report
        )

        if rel.using is not None:
            return self._build_using_scope(rel, left_scope, right_scope,
                                           depth, outer_chain, right_siblings), rel
        if rel.on is not None:
            chain = outer_chain + [left_scope, right_scope]
            rel.on = self.bind_expr(
                rel.on, chain, depth, report, forbidden
            )
        merged = self._merge_scopes(left_scope, right_scope, depth, rel,
                                    eq_pairs=self._extract_eq_pairs(
                                        rel.on, left_scope, right_scope))
        return merged, rel

    # -- USING -------------------------------------------------------------
    def _build_using_scope(self, join: Join, left: Scope, right: Scope,
                           depth, outer_chain, right_siblings):
        using = list(join.using)
        left_tbl = left.tables[0] if len(left.tables) == 1 else None
        right_tbl = right.tables[0] if len(right.tables) == 1 else None

        # 绑定合成等值条件（供执行器使用），并收集合并列
        eq_pairs = []
        merged_cols: list[ResolvedColumn] = []
        position = 0
        for uname in using:
            lmatches = left.columns.get(uname, [])
            rmatches = right.columns.get(uname, [])
            if len(lmatches) != 1 or len(rmatches) != 1:
                raise UnresolvedColumnError(
                    f"USING column {uname!r} must exist exactly once on each side",
                    column=uname, location="join-using",
                )
            lcol, rcol = lmatches[0], rmatches[0]
            lref = ColumnRef(table=lcol.qualifier, name=uname, resolved=lcol)
            rref = ColumnRef(table=rcol.qualifier, name=uname, resolved=rcol)
            eq_pairs.append((lcol, rcol))
            if join.join_type == "full":
                source = FuncCall("coalesce", [lref, rref])
            elif join.join_type == "right":
                source = FuncCall("coalesce", [lref, rref])
            else:  # inner / left：左侧值即合并值
                source = lref
            merged = ResolvedColumn(
                qualifier=None, name=uname, depth=depth,
                scope_position=position, table_scope_position=position,
                base_refs=tuple(sorted(set(lcol.base_refs + rcol.base_refs))),
                source=source, is_using_merged=True, using_name=uname,
            )
            merged_cols.append(merged)
            position += 1

        # 左右表符号保留全部原列（限定表名仍可访问），但从无限定可见集合中隐藏
        hidden = set(using)
        left_visible = [c for c in left.ordered if c.name not in hidden]
        right_visible = [c for c in right.ordered if c.name not in hidden]
        ordered = merged_cols + left_visible + right_visible
        for i, rc in enumerate(ordered):
            rc.scope_position = i

        tables = list(left.tables) + list(right.tables)
        columns: dict[str, list[ResolvedColumn]] = {}
        for rc in ordered:
            columns.setdefault(rc.name, []).append(rc)
        scope = Scope(tables=tables, ordered=ordered, columns=columns)
        scope.unique_keys = self._preserved_keys(
            left, right, join.join_type, eq_pairs, join.lateral
        )
        # 执行期需要的合并元数据挂在 Join 节点上
        join.using_meta = [
            {"name": m.using_name,
             "left_qualifier": p[0].qualifier, "right_qualifier": p[1].qualifier,
             "left_refs": tuple(p[0].base_refs), "right_refs": tuple(p[1].base_refs)}
            for m, p in zip(merged_cols, eq_pairs)
        ]
        join.bound_using = True
        return scope

    # -- 普通作用域合并 -----------------------------------------------------
    def _merge_scopes(self, left: Scope, right: Scope, depth, join: Join, eq_pairs):
        ordered = list(left.ordered) + list(right.ordered)
        for i, rc in enumerate(ordered):
            rc.scope_position = i
        columns: dict[str, list[ResolvedColumn]] = {}
        for rc in ordered:
            columns.setdefault(rc.name, []).append(rc)
        scope = Scope(
            tables=list(left.tables) + list(right.tables),
            ordered=ordered, columns=columns,
        )
        scope.unique_keys = self._preserved_keys(
            left, right, join.join_type, eq_pairs, join.lateral
        )
        return scope

    def _extract_eq_pairs(self, on_expr, left: Scope, right: Scope):
        pairs = []
        if on_expr is None:
            return pairs
        stack = [on_expr]
        while stack:
            e = stack.pop()
            if isinstance(e, FuncCall):
                if e.name == "and":
                    stack.extend(e.args)
                elif e.name == "=" and len(e.args) == 2:
                    a, b = e.args
                    if isinstance(a, ColumnRef) and isinstance(b, ColumnRef):
                        ra, rb = a.resolved, b.resolved
                        if self._in_scope(ra, left) and self._in_scope(rb, right):
                            pairs.append((ra, rb))
                        elif self._in_scope(ra, right) and self._in_scope(rb, left):
                            pairs.append((rb, ra))
        return pairs

    @staticmethod
    def _in_scope(rc: ResolvedColumn, scope: Scope):
        return any(rc in t.columns for t in scope.tables)

    def _preserved_keys(self, left: Scope, right: Scope, join_type,
                        eq_pairs, lateral):
        def covers(scope: Scope, side_cols):
            # side_cols 是连接等值中某一侧的 ResolvedColumn；
            # 唯一键的每一列由 base_refs 血缘集合标识
            refs_used = set()
            for c in side_cols:
                refs_used.update(c.base_refs)
            for key in scope.unique_keys:
                need = {part for key_col in key for part in key_col}
                if need and need <= refs_used:
                    return True
            return False

        right_covered = covers(right, [p[1] for p in eq_pairs])
        left_covered = covers(left, [p[0] for p in eq_pairs])
        result = []
        if join_type == "inner":
            # 右侧按其唯一键被匹配 -> 每个左行至多匹配一个右行，左键保留；反之亦然
            if right_covered:
                result.extend(left.unique_keys)
            if left_covered:
                result.extend(right.unique_keys)
        elif join_type == "left":
            # 左表行全部保留；右表按唯一键匹配时不放大左表
            result.extend(left.unique_keys)
        elif join_type == "right":
            result.extend(right.unique_keys)
        # full / cross：无键可保证
        dedup = []
        seen = set()
        for k in result:
            if k not in seen:
                seen.add(k)
                dedup.append(k)
        return dedup

    def _single_scope(self, sym: TableSym, depth, keys):
        columns: dict[str, list[ResolvedColumn]] = {}
        for rc in sym.columns:
            columns.setdefault(rc.name, []).append(rc)
        return Scope(tables=[sym], ordered=list(sym.columns),
                     columns=columns, unique_keys=list(keys))

    @staticmethod
    def _mark_sibling(scope: Scope) -> Scope:
        # 复用列与表符号，仅切换兄弟标记，避免污染外层自身作用域
        clone = Scope(
            tables=scope.tables, ordered=scope.ordered,
            columns=scope.columns, unique_keys=scope.unique_keys,
            is_sibling=True,
        )
        return clone

    # -- 表达式绑定 ---------------------------------------------------------
    def bind_expr(self, expr, chain, own_depth, report: BindReport, forbidden):
        if isinstance(expr, Literal):
            return expr
        if isinstance(expr, ColumnRef):
            return self._bind_column(expr, chain, own_depth, report, forbidden)
        if isinstance(expr, FuncCall):
            expr.args = [
                self.bind_expr(a, chain, own_depth, report, forbidden)
                for a in expr.args
            ]
            return expr
        if isinstance(expr, SubqueryExpr):
            expr.query = self.bind_select(expr.query, chain, forbidden)
            return expr
        if isinstance(expr, Star):
            raise UnresolvedColumnError(
                "star is only allowed at the top of a projection list",
                location="expression",
            )
        raise TypeError(f"unsupported expression: {type(expr)!r}")

    def _bind_column(self, ref: ColumnRef, chain, own_depth, report, forbidden):
        found, found_scope = self._resolve(ref, chain)
        if found is None:
            if self._visible_in(ref, forbidden):
                raise IllegalLateralError(
                    f"non-lateral derived table cannot reference left neighbour "
                    f"column {ref.table + '.' if ref.table else ''}{ref.name}",
                    qualifier=ref.table, column=ref.name, location=ref.location,
                )
            raise UnresolvedColumnError(
                f"unresolved column {ref.table + '.' if ref.table else ''}{ref.name}",
                qualifier=ref.table, column=ref.name, location=ref.location,
            )
        ref.resolved = found
        if found.depth < own_depth:
            report.free_vars.append(FreeVar(
                name=found.name, qualifier=found.qualifier, depth=found.depth,
                sibling=found_scope.is_sibling, location=ref.location,
                base_refs=found.base_refs,
            ))
        return ref

    def _resolve(self, ref: ColumnRef, chain: list[Scope]):
        if ref.table is not None:
            for scope in reversed(chain):
                tbl = scope.find_table(ref.table)
                if tbl is not None:
                    for rc in tbl.columns:
                        if rc.name == ref.name:
                            return rc, scope
                    return None, scope  # 限定表存在但列不存在：直接失败
            return None, None
        # 未限定：由内向外，第一个含该名的作用域胜出；同层多解即歧义
        for scope in reversed(chain):
            matches = scope.columns.get(ref.name)
            if matches:
                if len(matches) > 1:
                    candidates = [
                        {
                            "qualifier": c.qualifier, "column": c.name,
                            "depth": c.depth, "sibling": c.is_sibling,
                            "position": c.scope_position,
                            "base_refs": c.base_refs,
                        }
                        for c in matches
                    ]
                    raise AmbiguousColumnError(
                        f"column {ref.name!r} is ambiguous",
                        candidates=candidates, location=ref.location,
                    )
                return matches[0], scope
        return None, None

    @staticmethod
    def _visible_in(ref_or_exc, scopes):
        if isinstance(ref_or_exc, UnresolvedColumnError):
            qualifier, name = ref_or_exc.qualifier, ref_or_exc.column
        else:
            qualifier, name = ref_or_exc.table, ref_or_exc.name
        for scope in scopes:
            if qualifier is not None:
                tbl = scope.find_table(qualifier)
                if tbl and any(c.name == name for c in tbl.columns):
                    return True
            elif name in scope.columns:
                return True
        return False

    @staticmethod
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


def bind(query: SelectNode, catalog: Catalog) -> SelectNode:
    """绑定查询树，返回带 ResolvedColumn 与 BindReport 的新树（不改原树）。"""
    return Binder(catalog).bind_root(query)
