"""Flexible coercion for expressions, stats, and relation inputs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .expressions import (
    And,
    Column,
    Compare,
    Expr,
    IsNull,
    Literal,
    Not,
    Or,
    canonical_op,
)
from .parser import ExpressionParseError, parse_expr
from .stats import (
    ColumnStats,
    Relation,
    coerce_column_stats,
    coerce_relation,
    coerce_relations,
)


def as_expr(value: Any) -> Expr:
    """Coerce a string, AST node, dict, or tuple DSL into an :class:`Expr`.

    Tuple examples::

        ("gt", "x", 5)
        ("and", ("eq", "a", 1), ("is_null", "b"))
        ("or", [expr1, expr2, expr3])
    """
    if isinstance(value, Expr):
        return value
    if isinstance(value, str):
        # A bare column name is also a valid expression, but a string with
        # spaces/operators is unambiguously a filter expression.
        stripped = value.strip()
        if " " not in stripped and _looks_like_identifier(stripped):
            # Could be either a sole column reference or a keyword.  Parsing
            # handles keywords and quoted identifiers consistently.
            try:
                return parse_expr(stripped)
            except ExpressionParseError:
                return Column(stripped)
        return parse_expr(value)
    if isinstance(value, Mapping):
        return _expr_from_mapping(value)
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return _expr_from_sequence(value)
    if value is None or isinstance(value, (int, bool)):
        return Literal(value)
    raise TypeError(f"cannot coerce {type(value).__name__} to expression")


def _looks_like_identifier(text: str) -> bool:
    if not text:
        return False
    if text[0].isalpha() or text[0] == "_":
        return all(ch.isalnum() or ch == "_" for ch in text[1:])
    return False


def _operand(value: Any) -> Expr:
    if isinstance(value, str) and _looks_like_identifier(value):
        return Column(value)
    return as_expr(value)


def _expr_from_mapping(data: Mapping[str, Any]) -> Expr:
    op = data.get("op") or data.get("type")
    if op is None:
        raise ValueError(f"expression mapping requires 'op': {data!r}")
    op = str(op).lower()
    if op in ("and", "or"):
        args = data.get("args") or data.get("children") or []
        if not isinstance(args, Sequence):
            raise ValueError("and/or requires an args sequence")
        exprs = [as_expr(x) for x in args]
        result: Expr = Literal(op == "and")
        for expr in exprs:
            result = And(result, expr) if op == "and" else Or(result, expr)
        return result
    if op == "not":
        return Not(as_expr(data.get("operand") or data.get("child") or data.get("expr")))
    if op in ("is_null", "isnull"):
        return IsNull(as_expr(data.get("operand") or data.get("column") or data.get("expr")), False)
    if op in ("is_not_null", "isnotnull"):
        return IsNull(as_expr(data.get("operand") or data.get("column") or data.get("expr")), True)
    if op in ("col", "column"):
        return Column(str(data.get("name") or data.get("value")))
    if op in ("lit", "literal"):
        return Literal(data.get("value"))
    left = as_expr(data["left"])
    right = as_expr(data["right"])
    return Compare(left, canonical_op(op), right)


def _expr_from_sequence(node: Sequence[Any]) -> Expr:
    if not node:
        raise ValueError("empty expression tuple")
    tag = str(node[0]).lower()

    if tag in ("col", "column"):
        return Column(str(node[1]))
    if tag in ("lit", "literal"):
        return Literal(node[1])
    if tag == "null":
        return Literal(None)
    if tag in ("true", "false", "bool"):
        return Literal(tag == "true" if tag in ("true", "false") else bool(node[1]))

    if tag in ("and", "or"):
        if len(node) == 2 and isinstance(node[1], Sequence) and not isinstance(node[1], (str, bytes)):
            args = list(node[1])
        else:
            args = list(node[1:])
        if not args:
            return Literal(tag == "and")
        result = as_expr(args[0])
        for arg in args[1:]:
            right = as_expr(arg)
            result = And(result, right) if tag == "and" else Or(result, right)
        return result

    if tag == "not":
        return Not(as_expr(node[1]))
    if tag in ("is_null", "isnull"):
        return IsNull(_operand(node[1]), False)
    if tag in ("is_not_null", "isnotnull"):
        return IsNull(_operand(node[1]), True)

    # ("gt", left, right)
    if len(node) == 3:
        return Compare(_operand(node[1]), canonical_op(tag), _operand(node[2]))

    raise ValueError(f"unsupported expression tuple: {node!r}")


def coerce_column_mapping(columns: Any) -> dict[str, ColumnStats]:
    if columns is None:
        return {}
    if isinstance(columns, Mapping):
        result: dict[str, ColumnStats] = {}
        for name, value in columns.items():
            result[str(name)] = coerce_column_stats(value, column_name=str(name))
        return result
    raise TypeError("columns must be a mapping of column name to stats")
