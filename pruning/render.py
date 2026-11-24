"""Render AST nodes as reviewable SQL-like text."""

from __future__ import annotations

from typing import Any

from .expressions import (
    And,
    Arithmetic,
    Column,
    Compare,
    Expr,
    IsNull,
    Literal,
    Not,
    Or,
)


def render_expr(expr: Expr | Any) -> str:
    if isinstance(expr, str):
        return expr
    if isinstance(expr, Column):
        return expr.name
    if isinstance(expr, Literal):
        if expr.value is None:
            return "NULL"
        if expr.value is True:
            return "TRUE"
        if expr.value is False:
            return "FALSE"
        return str(expr.value)
    if isinstance(expr, Compare):
        return f"({render_expr(expr.left)} {expr.op} {render_expr(expr.right)})"
    if isinstance(expr, IsNull):
        inner = render_expr(expr.operand)
        return f"{inner} IS NOT NULL" if expr.negated else f"{inner} IS NULL"
    if isinstance(expr, Not):
        return f"NOT {render_atom(expr.operand)}"
    if isinstance(expr, And):
        return f"({render_expr(expr.left)} AND {render_expr(expr.right)})"
    if isinstance(expr, Or):
        return f"({render_expr(expr.left)} OR {render_expr(expr.right)})"
    if isinstance(expr, Arithmetic):
        return f"({render_expr(expr.left)} {expr.op} {render_expr(expr.right)})"
    return str(expr)


def render_atom(expr: Expr) -> str:
    text = render_expr(expr)
    if isinstance(expr, (And, Or)):
        return f"({text})"
    return text
