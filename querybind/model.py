"""查询树模型（输入树与绑定/改写后的树共用这些节点）。

关系（RelNode）：
    TableRef     基表，带别名
    DerivedTable 派生表（lateral=True 时为 LATERAL）
    Join         内连接/左/右/全外连接，支持 ON 或 USING，带 lateral 标记

表达式（Expr）：
    Literal / ColumnRef / FuncCall / Star / SubqueryExpr

绑定结果写在 ColumnRef.resolved（ResolvedColumn），SelectNode.report 携带
自由列报告。调用方一般使用模块级便捷构造器 col/lit/eq/...
"""

from dataclasses import dataclass, field
from typing import Any, Optional


# ---------------------------------------------------------------------------
# 表达式
# ---------------------------------------------------------------------------
@dataclass
class Expr:
    pass


@dataclass
class Literal(Expr):
    value: Any
    alias: Optional[str] = None


@dataclass
class ColumnRef(Expr):
    table: Optional[str]
    name: str
    alias: Optional[str] = None
    # 绑定阶段填充
    resolved: Optional[Any] = None  # type: ignore[assignment]
    location: Optional[str] = None

    def is_bound(self):
        return self.resolved is not None


@dataclass
class FuncCall(Expr):
    name: str
    args: list[Expr] = field(default_factory=list)
    alias: Optional[str] = None


@dataclass
class Star(Expr):
    # None 表示裸 *；字符串表示 t.*
    table: Optional[str] = None


@dataclass
class SubqueryExpr(Expr):
    query: Any  # SelectNode
    alias: Optional[str] = None
    # 改写阶段标记：已被提升为左连接
    decorrelated: bool = False
    join_alias: Optional[str] = None


# ---------------------------------------------------------------------------
# 关系
# ---------------------------------------------------------------------------
@dataclass
class RelNode:
    alias: Optional[str] = None


@dataclass
class TableRef(RelNode):
    name: str = ""
    alias: Optional[str] = None


@dataclass
class DerivedTable(RelNode):
    query: Any = None  # SelectNode
    alias: Optional[str] = None
    lateral: bool = False


@dataclass
class Join(RelNode):
    join_type: str = "inner"          # inner / left / right / full
    left: Optional[RelNode] = None
    right: Optional[RelNode] = None
    on: Optional[Expr] = None
    using: Optional[list[str]] = None
    lateral: bool = False
    alias: Optional[str] = None
    # 绑定阶段填充：USING 合并列的左右限定名信息
    bound_using: bool = False
    using_meta: Optional[list[dict]] = None
    # 改写阶段填充：该连接由去相关提升产生，已是未绑定树
    rewrite_emitted: bool = False

@dataclass
class SelectNode:
    projections: list[Expr] = field(default_factory=list)
    from_: Optional[RelNode] = None
    where: Optional[Expr] = None
    # 绑定阶段填充
    nid: Optional[int] = None
    report: Optional[Any] = None  # BindReport

    def is_scalar(self):
        """标量子查询要求输出单列。"""
        return len(self.projections) == 1


# ---------------------------------------------------------------------------
# 便捷构造器
# ---------------------------------------------------------------------------
def col(name, table=None, alias=None, location=None):
    if isinstance(name, tuple) and table is None:
        table, name = name
    return ColumnRef(table=table, name=name, alias=alias, location=location)


def lit(value, alias=None):
    return Literal(value=value, alias=alias)


def star(table=None):
    return Star(table=table)


def eq(a, b):
    return FuncCall("=", [a, b])


def and_(*preds):
    preds = [p for p in preds if p is not None]
    if not preds:
        return None
    result = preds[0]
    for p in preds[1:]:
        result = FuncCall("and", [result, p])
    return result


def or_(a, b):
    return FuncCall("or", [a, b])


def not_(a):
    return FuncCall("not", [a])


def subq(query, alias=None):
    return SubqueryExpr(query=query, alias=alias)
