"""对绑定树（或改写后重新绑定的树）执行小型行集求值。

环境为 list[dict]，每个 dict 以 (限定名, 列名) 为键，对应绑定深度：
    env[-1] 是当前查询块作用域，env[-2] 是直接外层，依此类推。
LATERAL 左邻作用域与当前块同深度，直接并入当前层 dict。
"""

from __future__ import annotations

from dataclasses import dataclass

from .binder import ResolvedColumn
from .catalog import Catalog
from .model import (
    ColumnRef,
    DerivedTable,
    FuncCall,
    Join,
    Literal,
    SelectNode,
    SubqueryExpr,
    TableRef,
)


@dataclass
class ResultColumn:
    name: str
    qualifier: object
    position: int


@dataclass
class QueryResult:
    columns: list[ResultColumn]
    # rows 按列位置存储（list[list]），允许同名输出列并存
    rows: list[list]

    def ordered_rows(self):
        """每行按输出列顺序返回值列表。"""
        return [list(row) for row in self.rows]

    def names(self):
        return [c.name for c in self.columns]

    def to_dicts(self):
        return [
            {c.name: row[i] for i, c in enumerate(self.columns)}
            for row in self.rows
        ]


def _null_safe(fn):
    def wrapped(*args):
        if any(a is None for a in args):
            return None
        return fn(*args)
    return wrapped


def _cmp(op):
    def fn(a, b):
        if a is None or b is None:
            return None
        return {
            "=": a == b, "<>": a != b, "!=": a != b,
            "<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b,
        }[op]
    return fn


class Executor:
    def __init__(self, catalog: Catalog):
        self.catalog = catalog

    def execute(self, select: SelectNode, outer_env=None) -> QueryResult:
        if select.nid is None:
            raise ValueError("execute() expects a bound query (call bind() first)")
        env_stack = list(outer_env or [])
        scope_rows = self._eval_rel(select.from_, env_stack) if select.from_ else [{}]
        if select.where is not None:
            scope_rows = [
                row for row in scope_rows
                if self._truthy(self._eval(select.where, env_stack + [row]))
            ]
        columns = [
            ResultColumn(name=p.alias, qualifier=self._qualifier_of(p), position=i)
            for i, p in enumerate(select.projections)
        ]
        rows = []
        for row in scope_rows:
            rows.append([
                self._eval(proj, env_stack + [row])
                for proj in select.projections
            ])
        return QueryResult(columns=columns, rows=rows)

    @staticmethod
    def _qualifier_of(proj):
        if isinstance(proj, ColumnRef):
            return proj.resolved.qualifier
        return None

    # -- 关系求值 -----------------------------------------------------------
    def _eval_rel(self, rel, env_stack, lateral_left=None):
        if isinstance(rel, TableRef):
            data = self.catalog.get(rel.name)
            alias = rel.alias or rel.name
            return [
                {(alias, c): row.get(c) for c in data.columns}
                for row in data.rows
            ]
        if isinstance(rel, DerivedTable):
            if rel.lateral:
                return self._eval_lateral_derived(rel, env_stack, lateral_left)
            result = self.execute(rel.query, env_stack)
            names = result.names()
            return [
                {(rel.alias, names[i]): row[i] for i in range(len(names))}
                for row in result.rows
            ]
        if isinstance(rel, Join):
            return self._eval_join(rel, env_stack, lateral_left)
        raise TypeError(f"unsupported relation: {type(rel)!r}")

    def _eval_lateral_derived(self, rel: DerivedTable, env_stack, lateral_left):
        # LATERAL 必须接在某个左邻关系之后；左邻单行作为同深度环境传入
        if lateral_left is None:
            raise RuntimeError("LATERAL derived table requires a left neighbour")
        inner_result = self.execute(rel.query, env_stack + [lateral_left])
        out_rows = []
        if not inner_result.rows:
            # 零行占位：保留左邻列并预填右侧空键，左外连接即可直接补空
            placeholder = dict(lateral_left)
            for c in inner_result.columns:
                placeholder.setdefault((rel.alias, c.name), None)
            out_rows.append(placeholder)
            return out_rows
        for inner in inner_result.rows:
            merged = dict(lateral_left)
            for i, c in enumerate(inner_result.columns):
                merged[(rel.alias, c.name)] = inner[i]
            out_rows.append(merged)
        return out_rows

    def _eval_join(self, join: Join, env_stack, lateral_left=None):
        left_rows = self._eval_rel(join.left, env_stack, lateral_left)
        right_rows_per_left = []
        for lrow in left_rows:
            if join.lateral or self._is_lateral(join.right):
                rrows = self._eval_rel(join.right, env_stack, lrow)
            else:
                rrows = self._eval_rel(join.right, env_stack)
            right_rows_per_left.append(rrows)
        return self._join(join, left_rows, right_rows_per_left, env_stack)

    @staticmethod
    def _is_lateral(rel):
        if isinstance(rel, DerivedTable):
            return rel.lateral
        if isinstance(rel, Join):
            return rel.lateral or Executor._is_lateral(rel.right)
        return False

    def _is_lateral_placeholder(self, join, rrow):
        # 占位行由 LATERAL 零匹配产生：包含右侧别名键且值全为 None
        right_aliases = self._right_aliases(join.right)
        if not right_aliases:
            return False
        right_keys = [k for k in rrow if k[0] in right_aliases]
        return bool(right_keys) and all(rrow[k] is None for k in right_keys)

    def _right_aliases(self, rel):
        if isinstance(rel, (TableRef, DerivedTable)):
            return {rel.alias} if rel.alias else set()
        if isinstance(rel, Join):
            return self._right_aliases(rel.right)
        return set()

    def _join(self, join: Join, left_rows, right_rows_per_left, env_stack):
        using_meta = join.using_meta or []
        # 非 LATERAL 时每个左行共享同一右侧集合；LATERAL 时各自独立
        static_right = not (join.lateral or self._is_lateral(join.right))
        if static_right:
            canonical_right = right_rows_per_left[0] if right_rows_per_left else []
            matched = [False] * len(canonical_right)
        else:
            matched = None
        result = []

        for li, lrow in enumerate(left_rows):
            rrows = right_rows_per_left[li]
            any_match = False
            local_matched = [False] * len(rrows)
            for ri, rrow in enumerate(rrows):
                if self._is_lateral_placeholder(join, rrow):
                    # LATERAL 零行占位：内连接丢弃，左/全外连接走补空分支
                    continue
                if join.using:
                    ok = self._using_match(join, lrow, rrow)
                elif join.on is not None:
                    ok = self._truthy(self._eval(join.on, env_stack + [
                        self._merge_row(lrow, rrow)
                    ]))
                else:
                    ok = True
                if not ok:
                    continue
                any_match = True
                local_matched[ri] = True
                if static_right:
                    matched[ri] = True
                result.append(self._merge_row(lrow, rrow, join, using_meta))
            if not any_match and join.join_type in ("left", "full"):
                result.append(self._extend_nulls(lrow, rrows,
                                                 side="left", join=join,
                                                 using_meta=using_meta))
            if not static_right and join.join_type in ("right", "full"):
                # LATERAL：右侧集合按左行独立，未匹配右行就地补左空发出
                for ri, rrow in enumerate(rrows):
                    if not local_matched[ri]:
                        result.append(self._extend_nulls(rrow, [lrow],
                                                         side="right",
                                                         join=join,
                                                         using_meta=using_meta))

        if static_right and join.join_type in ("right", "full"):
            for ri, rrow in enumerate(canonical_right):
                if not matched[ri]:
                    result.append(self._extend_nulls(
                        rrow, left_rows, side="right", join=join,
                        using_meta=using_meta,
                    ))
        return result

    @staticmethod
    def _merge_row(lrow, rrow, join=None, using_meta=None):
        merged = dict(lrow)
        if join is not None and join.using:
            full_or_right = join.join_type in ("full", "right")
            for meta in using_meta:
                lk = (meta["left_qualifier"], meta["name"])
                rk = (meta["right_qualifier"], meta["name"])
                lv, rv = lrow.get(lk), rrow.get(rk)
                # 左右原键各自保留，限定表名仍可访问
                merged[lk] = lv
                merged[rk] = rv
                # 合并键单独存放在哨兵键 (None, name) 下，不覆盖原键
                if full_or_right:
                    merged[(None, meta["name"])] = (
                        lv if lv is not None else rv
                    )
                else:
                    merged[(None, meta["name"])] = lv
            for k, v in rrow.items():
                if k not in merged:
                    merged[k] = v
        else:
            for k, v in rrow.items():
                merged[k] = v
        return merged

    def _using_match(self, join: Join, lrow, rrow):
        for meta in join.using_meta:
            lv = lrow.get((meta["left_qualifier"], meta["name"]))
            rv = rrow.get((meta["right_qualifier"], meta["name"]))
            if lv is None or rv is None or lv != rv:
                return False
        return True

    def _extend_nulls(self, row, other_rows, side, join, using_meta):
        out = dict(row)
        sample = other_rows[0] if other_rows else {}
        if join is None or not join.using:
            for k in sample:
                out.setdefault(k, None)
            return out
        if side == "left":
            for k in sample:
                out.setdefault(k, None)
            for meta in using_meta:
                lk = (meta["left_qualifier"], meta["name"])
                rk = (meta["right_qualifier"], meta["name"])
                lv = row.get(lk)
                out[lk] = lv
                out[rk] = None
                out[(None, meta["name"])] = lv
        else:
            for k in sample:
                out.setdefault(k, None)
            for meta in using_meta:
                lk = (meta["left_qualifier"], meta["name"])
                rk = (meta["right_qualifier"], meta["name"])
                rv = row.get(rk)
                out[lk] = None
                out[rk] = rv
                out[(None, meta["name"])] = rv
        return out

    # -- 表达式求值 ---------------------------------------------------------
    def _eval(self, expr, env_stack):
        if isinstance(expr, Literal):
            return expr.value
        if isinstance(expr, ColumnRef):
            return self._lookup(expr.resolved, env_stack)
        if isinstance(expr, SubqueryExpr):
            return self._eval_scalar_subquery(expr, env_stack)
        if isinstance(expr, FuncCall):
            return self._eval_func(expr, env_stack)
        raise TypeError(f"unsupported expression: {type(expr)!r}")

    def _lookup(self, rc: ResolvedColumn, env_stack):
        # USING 合并列：优先读哨兵键；其 source 表达式给出首个非空来源
        if rc.is_using_merged:
            sentinel = (None, rc.name)
            for env in reversed(env_stack):
                if sentinel in env:
                    return env[sentinel]
            if rc.source is not None:
                return self._eval(rc.source, env_stack)
        key = (rc.qualifier, rc.name)
        for env in reversed(env_stack):
            if key in env:
                return env[key]
        raise RuntimeError(
            f"no runtime value for {rc.qualifier}.{rc.name} (depth {rc.depth})"
        )

    def _eval_scalar_subquery(self, expr: SubqueryExpr, env_stack):
        result = self.execute(expr.query, env_stack)
        if not result.rows:
            return None
        if len(result.rows) > 1:
            raise RuntimeError("scalar subquery returned more than one row")
        return result.rows[0][0]

    def _eval_func(self, expr: FuncCall, env_stack):
        name = expr.name
        args = [self._eval(a, env_stack) for a in expr.args]
        builtins = self._builtins()
        if name not in builtins:
            raise RuntimeError(f"unsupported function/operator: {name}")
        return builtins[name](*args)

    @staticmethod
    def _builtins():
        return {
            "=": _cmp("="), "<>": _cmp("<>"), "!=": _cmp("!="),
            "<": _cmp("<"), "<=": _cmp("<="), ">": _cmp(">"), ">=": _cmp(">="),
            "and": lambda a, b: Executor._logic(a, b, True),
            "or": lambda a, b: Executor._logic(a, b, False),
            "not": lambda a: None if a is None else (not a),
            "+": _null_safe(lambda a, b: a + b),
            "-": _null_safe(lambda a, b: a - b),
            "*": _null_safe(lambda a, b: a * b),
            "/": _null_safe(lambda a, b: a / b),
            "coalesce": lambda *xs: next((x for x in xs if x is not None), None),
            "is_null": lambda a: a is None,
        }

    @staticmethod
    def _logic(a, b, is_and):
        if a is None and b is None:
            return None
        if is_and:
            if a is False or b is False:
                return False
            if a is None or b is None:
                return None
            return bool(a and b)
        if a is True or b is True:
            return True
        if a is None or b is None:
            return None
        return bool(a or b)

    @staticmethod
    def _truthy(value):
        return value is True
