"""Expression AST and constructor helpers for row-group pruning.

The AST is intentionally small.  It covers integer comparisons, SQL null
predicates, boolean literals, and ``AND`` / ``OR`` / ``NOT`` composition.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


COMPARISON_OPERATORS = ("=", "!=", "<", "<=", ">", ">=")

_OP_ALIASES = {
    "eq": "=",
    "==": "=",
    "=": "=",
    "ne": "!=",
    "ne!=": "!=",
    "!=": "!=",
    "<>": "!=",
    "lt": "<",
    "<": "<",
    "le": "<=",
    "<=": "<=",
    "gt": ">",
    ">": ">",
    "ge": ">=",
    ">=": ">=",
}


def canonical_op(op: Any) -> str:
    key = str(op).strip().lower()
    if key not in _OP_ALIASES:
        raise ValueError(f"unsupported comparison operator: {op!r}")
    return _OP_ALIASES[key]


def _operand(value: Any) -> "Expr":
    if isinstance(value, Expr):
        return value
    if value is None or isinstance(value, (int, bool)):
        return Literal(value)
    if isinstance(value, str):
        # In builder expressions a bare string is a column name.
        return Column(value)
    if isinstance(value, (tuple, list, dict)):
        from .coercion import as_expr

        return as_expr(value)
    raise TypeError(f"invalid comparison operand: {value!r}")


class Expr:
    """Base class for all expression nodes.

    Python comparison/bitwise operators are overloaded as expression builders.
    Because ``==`` must build a SQL comparison, AST nodes do not use structural
    equality; use :func:`ast_repr` in tests or inspect dataclass fields.
    """

    __slots__ = ()

    def __lt__(self, other: Any) -> "Compare":
        return Compare(self, "<", _operand(other))

    def __le__(self, other: Any) -> "Compare":
        return Compare(self, "<=", _operand(other))

    def __gt__(self, other: Any) -> "Compare":
        return Compare(self, ">", _operand(other))

    def __ge__(self, other: Any) -> "Compare":
        return Compare(self, ">=", _operand(other))

    def __eq__(self, other: Any) -> "Compare":  # type: ignore[override]
        return Compare(self, "=", _operand(other))

    def __ne__(self, other: Any) -> "Compare":  # type: ignore[override]
        return Compare(self, "!=", _operand(other))

    def __and__(self, other: Any) -> "And":
        return And(self, _operand(other))

    def __or__(self, other: Any) -> "Or":
        return Or(self, _operand(other))

    def __rand__(self, other: Any) -> "And":
        return And(_operand(other), self)

    def __ror__(self, other: Any) -> "Or":
        return Or(_operand(other), self)

    def __invert__(self) -> "Not":
        return Not(self)

    # Explicit, readable aliases for callers who avoid operator overloading.
    def lt(self, other: Any) -> "Compare":
        return self < other

    def le(self, other: Any) -> "Compare":
        return self <= other

    def gt(self, other: Any) -> "Compare":
        return self > other

    def ge(self, other: Any) -> "Compare":
        return self >= other

    def eq(self, other: Any) -> "Compare":
        return self == other

    def ne(self, other: Any) -> "Compare":
        return self != other


@dataclass(frozen=True, eq=False)
class Column(Expr):
    name: str

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("column name must be a non-empty string")

    def __hash__(self) -> int:
        return hash(("Column", self.name))


@dataclass(frozen=True, eq=False)
class Literal(Expr):
    value: Any

    def __hash__(self) -> int:
        return hash(("Literal", repr(self.value)))


@dataclass(frozen=True, eq=False)
class Compare(Expr):
    left: Expr
    op: str
    right: Expr

    def __post_init__(self) -> None:
        object.__setattr__(self, "op", canonical_op(self.op))

    def __hash__(self) -> int:
        return id(self)


@dataclass(frozen=True, eq=False)
class IsNull(Expr):
    operand: Expr
    negated: bool = False

    def __hash__(self) -> int:
        return id(self)


@dataclass(frozen=True, eq=False)
class Not(Expr):
    operand: Expr

    def __hash__(self) -> int:
        return id(self)


@dataclass(frozen=True, eq=False)
class And(Expr):
    left: Expr
    right: Expr

    def __hash__(self) -> int:
        return id(self)


@dataclass(frozen=True, eq=False)
class Or(Expr):
    left: Expr
    right: Expr

    def __hash__(self) -> int:
        return id(self)


@dataclass(frozen=True, eq=False)
class Arithmetic(Expr):
    left: Expr
    op: str
    right: Expr

    def __post_init__(self) -> None:
        if self.op not in ("+", "-"):
            raise ValueError(f"unsupported arithmetic operator: {self.op!r}")

    def __hash__(self) -> int:
        return id(self)


def col(name: str) -> Column:
    """Create a column reference."""
    return Column(name)


def column(name: str) -> Column:
    """Alias for :func:`col`."""
    return Column(name)


def lit(value: Any) -> Literal:
    """Create a literal (integer, boolean, or NULL)."""
    return Literal(value)


def is_null(operand: Any) -> IsNull:
    return IsNull(_operand(operand), False)


def is_not_null(operand: Any) -> IsNull:
    return IsNull(_operand(operand), True)


def not_(operand: Any) -> Not:
    return Not(_operand(operand))


def and_(*conditions: Any) -> Expr:
    """Combine conditions with SQL AND (left-associative)."""
    exprs = [_operand(c) for c in conditions]
    if not exprs:
        return Literal(True)
    result = exprs[0]
    for expr in exprs[1:]:
        result = And(result, expr)
    return result


def or_(*conditions: Any) -> Expr:
    """Combine conditions with SQL OR (left-associative)."""
    exprs = [_operand(c) for c in conditions]
    if not exprs:
        return Literal(False)
    result = exprs[0]
    for expr in exprs[1:]:
        result = Or(result, expr)
    return result


def referenced_columns(expr: Expr) -> set[str]:
    """Return all column names occurring in *expr*."""
    found: set[str] = set()
    stack = [expr]
    while stack:
        node = stack.pop()
        if isinstance(node, Column):
            found.add(node.name)
        elif isinstance(node, Literal):
            pass
        elif isinstance(node, Compare):
            stack.extend((node.left, node.right))
        elif isinstance(node, IsNull):
            stack.append(node.operand)
        elif isinstance(node, Not):
            stack.append(node.operand)
        elif isinstance(node, (And, Or)):
            stack.extend((node.left, node.right))
        elif isinstance(node, Arithmetic):
            stack.extend((node.left, node.right))
        else:
            raise TypeError(f"unknown expression node: {type(node).__name__}")
    return found


def ast_repr(expr: Expr) -> str:
    """Stable compact representation useful in tests and debugging."""
    if isinstance(expr, Column):
        return f"Column({expr.name!r})"
    if isinstance(expr, Literal):
        return f"Literal({expr.value!r})"
    if isinstance(expr, Compare):
        return f"Compare({ast_repr(expr.left)} {expr.op} {ast_repr(expr.right)})"
    if isinstance(expr, IsNull):
        inner = ast_repr(expr.operand)
        return f"IsNotNull({inner})" if expr.negated else f"IsNull({inner})"
    if isinstance(expr, Not):
        return f"Not({ast_repr(expr.operand)})"
    if isinstance(expr, And):
        return f"And({ast_repr(expr.left)}, {ast_repr(expr.right)})"
    if isinstance(expr, Or):
        return f"Or({ast_repr(expr.left)}, {ast_repr(expr.right)})"
    if isinstance(expr, Arithmetic):
        return f"Arithmetic({ast_repr(expr.left)} {expr.op} {ast_repr(expr.right)})"
    return repr(expr)


def walk_expr(expr: Expr) -> Iterable[Expr]:
    yield expr
    children: list[Expr]
    if isinstance(expr, (Compare, And, Or, Arithmetic)):
        children = [expr.left, expr.right]
    elif isinstance(expr, (IsNull, Not)):
        children = [expr.operand]
    else:
        children = []
    for child in children:
        yield from walk_expr(child)
