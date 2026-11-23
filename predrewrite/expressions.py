"""Filter expressions and three-valued logic.

The supported grammar matches the task statement:

* :class:`Column`   -- qualified ``alias.column`` reference
* :class:`Literal`  -- scalar constant, including the SQL ``NULL`` sentinel
* :class:`Compare`  -- equality / ordering comparison (``= <> < <= > >=``)
* :class:`IsNull`   -- ``x IS NULL`` / ``x IS NOT NULL``
* :class:`Not`      -- logical negation
* :class:`And`      -- logical conjunction (n-ary, flat)
* :class:`Or`       -- logical disjunction (n-ary, flat)

Truth values use SQL three-valued logic (3VL): Python ``True`` / ``False``
plus ``None`` representing UNKNOWN.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

__all__ = [
    "Expr",
    "Column",
    "Literal",
    "Compare",
    "IsNull",
    "Not",
    "And",
    "Or",
    "TRUE",
    "FALSE",
    "NULL",
    "eval_expr",
    "is_null_rejecting",
    "split_conjuncts",
    "and_exprs",
    "referenced_aliases",
    "expr_to_text",
]

# SQL NULL / UNKNOWN marker (used both as a literal value and a 3VL truth
# value, exactly as in SQL where UNKNOWN and NULL share the same marker).
NULL: None = None


class Expr:
    """Abstract base class for all filter expressions."""

    # ------------------------------------------------------------------
    # Lightweight structural helpers (subclasses are frozen dataclasses).
    # ------------------------------------------------------------------
    def __and__(self, other: "Expr") -> "And":
        return and_exprs([self, other])

    def __or__(self, other: "Expr") -> "Or":
        parts: list[Expr] = []
        for e in (self, other):
            if isinstance(e, Or):
                parts.extend(e.parts)
            else:
                parts.append(e)
        return Or(tuple(parts))

    def __invert__(self) -> "Not":
        return Not(self)


@dataclass(frozen=True)
class Column(Expr):
    """A qualified column reference ``alias.column``."""

    alias: str
    name: str

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.alias}.{self.name}"


@dataclass(frozen=True)
class Literal(Expr):
    """A constant. ``value is None`` represents SQL NULL."""

    value: Any

    def __str__(self) -> str:  # pragma: no cover - trivial
        return "NULL" if self.value is None else repr(self.value)


@dataclass(frozen=True)
class Compare(Expr):
    """Comparison ``left op right``.

    ``op`` is one of ``"="``, ``"<>"``, ``"<"``, ``"<="``, ``">"``, ``">="``.
    Any operand may be a :class:`Column` or :class:`Literal`; comparisons
    between two columns (join predicates written as filters) are supported.
    """

    left: Expr
    op: str
    right: Expr

    def __post_init__(self) -> None:
        if self.op not in ("=", "<>", "<", "<=", ">", ">="):
            raise ValueError(f"unsupported comparison operator: {self.op!r}")


@dataclass(frozen=True)
class IsNull(Expr):
    """``arg IS NULL`` when ``negated`` is false, ``arg IS NOT NULL`` otherwise."""

    arg: Expr
    negated: bool = False


@dataclass(frozen=True)
class Not(Expr):
    """Logical NOT (3VL)."""

    arg: Expr


@dataclass(frozen=True)
class And(Expr):
    """Flat n-ary logical AND."""

    parts: tuple[Expr, ...]

    def __init__(self, parts: Iterable[Expr]) -> None:
        flat: list[Expr] = []
        for p in parts:
            if isinstance(p, And):
                flat.extend(p.parts)
            else:
                flat.append(p)
        if not flat:
            raise ValueError("And requires at least one conjunct")
        object.__setattr__(self, "parts", tuple(flat))


@dataclass(frozen=True)
class Or(Expr):
    """Flat n-ary logical OR."""

    parts: tuple[Expr, ...]

    def __init__(self, parts: Iterable[Expr]) -> None:
        flat: list[Expr] = []
        for p in parts:
            if isinstance(p, Or):
                flat.extend(p.parts)
            else:
                flat.append(p)
        if not flat:
            raise ValueError("Or requires at least one disjunct")
        object.__setattr__(self, "parts", tuple(flat))


# Convenient constant expressions (defined after the classes they use).
TRUE = Literal(True)
FALSE = Literal(False)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def split_conjuncts(expr: Expr) -> list[Expr]:
    """Flatten a top-level AND chain into its individual conjuncts."""
    return list(expr.parts) if isinstance(expr, And) else [expr]


def and_exprs(exprs: Iterable[Expr]) -> Expr:
    """Combine conjuncts back into one expression (flattening nested ANDs)."""
    items = list(exprs)
    if not items:
        raise ValueError("and_exprs requires at least one expression")
    if len(items) == 1:
        return items[0]
    return And(items)


def referenced_aliases(expr: Expr) -> frozenset[str]:
    """Return the set of table aliases referenced anywhere in ``expr``."""
    if isinstance(expr, Column):
        return frozenset({expr.alias})
    if isinstance(expr, Literal):
        return frozenset()
    if isinstance(expr, Compare):
        return referenced_aliases(expr.left) | referenced_aliases(expr.right)
    if isinstance(expr, IsNull):
        return referenced_aliases(expr.arg)
    if isinstance(expr, Not):
        return referenced_aliases(expr.arg)
    if isinstance(expr, (And, Or)):
        result: frozenset[str] = frozenset()
        for part in expr.parts:
            result |= referenced_aliases(part)
        return result
    raise TypeError(f"unknown expression type: {type(expr)!r}")  # pragma: no cover


# ---------------------------------------------------------------------------
# Three-valued logic
# ---------------------------------------------------------------------------

def _not3(v: Any) -> Any:
    if v is None:
        return None
    return not v


def _and3(values: Iterable[Any]) -> Any:
    """SQL AND over 3VL values (True / False / None(UNKNOWN))."""
    result: Any = True
    saw_unknown = False
    for v in values:
        if v is False:
            return False
        if v is None:
            saw_unknown = True
        elif v is True:
            pass
        else:  # pragma: no cover - defensive
            raise TypeError(f"not a 3VL truth value: {v!r}")
        result = v if result is True else result
    return None if saw_unknown else True


def _or3(values: Iterable[Any]) -> Any:
    """SQL OR over 3VL values (True / False / None(UNKNOWN))."""
    saw_unknown = False
    for v in values:
        if v is True:
            return True
        if v is None:
            saw_unknown = True
        elif v is False:
            pass
        else:  # pragma: no cover - defensive
            raise TypeError(f"not a 3VL truth value: {v!r}")
    return None if saw_unknown else False


def _compare(left: Any, op: str, right: Any) -> Any:
    # SQL: any NULL operand makes the comparison UNKNOWN.
    if left is None or right is None:
        return None
    if op == "=":
        return left == right
    if op == "<>":
        return left != right
    if op == "<":
        return left < right
    if op == "<=":
        return left <= right
    if op == ">":
        return left > right
    if op == ">=":
        return left >= right
    raise ValueError(f"unsupported operator: {op!r}")  # pragma: no cover


def eval_expr(expr: Expr, row: dict[tuple[str, str], Any]) -> Any:
    """Evaluate ``expr`` against one row.

    Rows map ``(alias, column)`` pairs to values; missing or explicit-NULL
    columns both read back as SQL NULL. The result is one of
    ``True`` / ``False`` / ``None`` (UNKNOWN).
    """
    if isinstance(expr, Column):
        return row.get((expr.alias, expr.name))
    if isinstance(expr, Literal):
        return expr.value
    if isinstance(expr, Compare):
        return _compare(
            eval_expr(expr.left, row), expr.op, eval_expr(expr.right, row)
        )
    if isinstance(expr, IsNull):
        value = eval_expr(expr.arg, row)
        is_null = value is None
        return (not is_null) if expr.negated else is_null
    if isinstance(expr, Not):
        return _not3(eval_expr(expr.arg, row))
    if isinstance(expr, And):
        return _and3(eval_expr(p, row) for p in expr.parts)
    if isinstance(expr, Or):
        return _or3(eval_expr(p, row) for p in expr.parts)
    raise TypeError(f"unknown expression type: {type(expr)!r}")  # pragma: no cover


# ---------------------------------------------------------------------------
# Symbolic null-rejection proof
# ---------------------------------------------------------------------------
#
# To decide whether a conjunct that references the null-supplied side of a
# left outer join can turn that join into an inner join, we evaluate the
# expression under a *symbolic* row:
#
#   * every column of the null-supplied alias is bound to NULL;
#   * every other column is bound to the sentinel ``_FREE`` ("some concrete
#     value, unknown to the prover").
#
# The evaluation returns a concrete 3VL value (True / False / None) whenever
# the result is independent of the free columns, or ``_FREE`` when the
# expression cannot be decided purely from the NULL padding.
#
# * concrete False or None (UNKNOWN) -> on every null-padded row the filter
#   rejects the row (a WHERE clause keeps only TRUE). The outer join's
#   padding rows are therefore removed by the filter, which is exactly the
#   inner-join semantics: the predicate is *null-rejecting*.
# * concrete True -> padding rows survive, the join must stay outer.
# * _FREE -> not provable from the expression alone; the predicate must be
#   retained above the join (conservative, always sound).

_FREE = object()


def _null_status_not(v: Any) -> Any:
    if v is _FREE:
        return _FREE
    return _not3(v)


def _null_status_and(values: list[Any]) -> Any:
    # Concrete 3VL whenever every component is concrete, otherwise propagate
    # _FREE (a free component could itself be True/False/UNKNOWN).
    if any(v is _FREE for v in values):
        return _FREE
    return _and3(values)


def _null_status_or(values: list[Any]) -> Any:
    if any(v is _FREE for v in values):
        return _FREE
    return _or3(values)


def _null_status_eval(expr: Expr, null_aliases: frozenset[str]) -> Any:
    if isinstance(expr, Column):
        return NULL if expr.alias in null_aliases else _FREE
    if isinstance(expr, Literal):
        return expr.value
    if isinstance(expr, Compare):
        lv = _null_status_eval(expr.left, null_aliases)
        rv = _null_status_eval(expr.right, null_aliases)
        if lv is _FREE or rv is _FREE:
            return _FREE
        return _compare(lv, expr.op, rv)
    if isinstance(expr, IsNull):
        v = _null_status_eval(expr.arg, null_aliases)
        if v is _FREE:
            # The column belongs to a preserved side and keeps its actual
            # value, which may itself be NULL, so IS NULL / IS NOT NULL is
            # undecidable from the padding information alone.
            return _FREE
        is_null = v is None
        return (not is_null) if expr.negated else is_null
    if isinstance(expr, Not):
        return _null_status_not(_null_status_eval(expr.arg, null_aliases))
    if isinstance(expr, And):
        return _null_status_and(
            [_null_status_eval(p, null_aliases) for p in expr.parts]
        )
    if isinstance(expr, Or):
        return _null_status_or(
            [_null_status_eval(p, null_aliases) for p in expr.parts]
        )
    raise TypeError(f"unknown expression type: {type(expr)!r}")  # pragma: no cover


def is_null_rejecting(expr: Expr, null_aliases: Iterable[str]) -> bool:
    """Return True when ``expr`` is provably not TRUE on null-padded rows.

    ``null_aliases`` are the aliases whose columns are all NULL on an outer
    join's padding rows. The predicate is null-rejecting iff its symbolic
    value under that padding is provably FALSE or UNKNOWN -- meaning a WHERE
    filter (which keeps only TRUE) removes every padding row, so the outer
    join is equivalent to an inner join.
    """
    status = _null_status_eval(expr, frozenset(null_aliases))
    return status is False or status is None


def null_status_explanation(expr: Expr, null_aliases: Iterable[str]) -> str:
    """Human-readable explanation of the symbolic proof, used in traces."""
    status = _null_status_eval(expr, frozenset(null_aliases))
    if status is False:
        return "evaluates to FALSE on null-padded rows"
    if status is None:
        return "evaluates to UNKNOWN on null-padded rows (WHERE keeps TRUE only)"
    if status is True:
        return "evaluates to TRUE on null-padded rows (padding rows survive)"
    return "depends on non-null columns; not provable from the expression alone"


# ---------------------------------------------------------------------------
# Text rendering (diagnostics / README examples)
# ---------------------------------------------------------------------------

def expr_to_text(expr: Expr) -> str:
    """Render an expression in SQL-like text form."""
    if isinstance(expr, Column):
        return f"{expr.alias}.{expr.name}"
    if isinstance(expr, Literal):
        return "NULL" if expr.value is None else repr(expr.value)
    if isinstance(expr, Compare):
        return f"({expr_to_text(expr.left)} {expr.op} {expr_to_text(expr.right)})"
    if isinstance(expr, IsNull):
        suffix = "IS NOT NULL" if expr.negated else "IS NULL"
        return f"({expr_to_text(expr.arg)} {suffix})"
    if isinstance(expr, Not):
        return f"NOT ({expr_to_text(expr.arg)})"
    if isinstance(expr, And):
        return "(" + " AND ".join(expr_to_text(p) for p in expr.parts) + ")"
    if isinstance(expr, Or):
        return "(" + " OR ".join(expr_to_text(p) for p in expr.parts) + ")"
    raise TypeError(f"unknown expression type: {type(expr)!r}")  # pragma: no cover
