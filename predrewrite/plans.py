"""Logical query-plan nodes and a bag-semantics evaluator.

Plan tree grammar::

    Plan   := Scan | Filter | InnerJoin | LeftOuterJoin
    Scan   := base table bound to an alias
    Filter := WHERE-style predicate above another plan
    Join   := InnerJoin | LeftOuterJoin, each carrying an *ON* expression

Distinction required by the task:

* the join **ON** expression is the *match condition* -- rows failing it
  (FALSE or UNKNOWN under 3VL) simply do not match, and for a left outer
  join a left row with no match is kept with right-side NULL padding;
* a :class:`Filter` above a join is a *post-join WHERE condition* -- it sees
  the null-padded rows and may therefore remove them.

The evaluator implements SQL bag semantics: duplicate input rows produce
duplicate output rows, and nothing is ever deduplicated.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .expressions import Expr, eval_expr, expr_to_text, split_conjuncts, and_exprs

__all__ = [
    "Plan",
    "Scan",
    "Filter",
    "InnerJoin",
    "LeftOuterJoin",
    "Table",
    "evaluate_plan",
    "plan_schema",
    "plan_to_text",
    "Row",
]

# A row maps qualified ``(alias, column)`` names to values; ``None`` is NULL.
Row = dict[tuple[str, str], Any]


class Plan:
    """Abstract base class for logical plan nodes."""

    node_id: str


@dataclass(frozen=True)
class Table:
    """A named base relation.

    ``rows`` are plain dictionaries keyed by *unqualified* column names;
    values may be ``None`` (NULL). Duplicate rows are allowed and preserved.
    """

    name: str
    columns: tuple[str, ...]
    rows: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class Scan(Plan):
    """Scan of a base table bound to ``alias``."""

    alias: str
    table: Table
    node_id: str = "scan"

    def qualified_rows(self) -> list[Row]:
        return [
            {(self.alias, col): row.get(col) for col in self.table.columns}
            for row in self.table.rows
        ]


@dataclass(frozen=True)
class Filter(Plan):
    """WHERE-style filter.

    ``pids`` records the ids of the original top-level conjuncts represented
    by ``expr`` (one id per conjunct, in order); it is bookkeeping for the
    rewrite trace.
    """

    source: Plan
    expr: Expr
    pids: tuple[str, ...] = ()
    node_id: str = "filter"


@dataclass(frozen=True)
class InnerJoin(Plan):
    left: Plan
    right: Plan
    on: Expr
    node_id: str = "inner_join"


@dataclass(frozen=True)
class LeftOuterJoin(Plan):
    """Left outer join: unmatched left rows survive with NULL-padded right."""

    left: Plan
    right: Plan
    on: Expr
    node_id: str = "left_outer_join"


# ---------------------------------------------------------------------------
# Schema helper
# ---------------------------------------------------------------------------

def plan_schema(plan: Plan) -> list[tuple[str, str]]:
    """Return the ordered output columns ``(alias, column)`` of ``plan``."""
    if isinstance(plan, Scan):
        return [(plan.alias, c) for c in plan.table.columns]
    if isinstance(plan, Filter):
        return plan_schema(plan.source)
    if isinstance(plan, (InnerJoin, LeftOuterJoin)):
        return plan_schema(plan.left) + plan_schema(plan.right)
    raise TypeError(f"unknown plan type: {type(plan)!r}")  # pragma: no cover


# ---------------------------------------------------------------------------
# Bag-semantics evaluation with SQL three-valued logic
# ---------------------------------------------------------------------------

def _passes(expr: Expr, row: Row) -> bool:
    """A WHERE / ON condition passes only when it evaluates to TRUE."""
    return eval_expr(expr, row) is True


def _merge(left_row: Row, right_row: Row) -> Row:
    merged: Row = dict(left_row)
    merged.update(right_row)
    return merged


def _evaluate(plan: Plan) -> list[Row]:
    if isinstance(plan, Scan):
        return plan.qualified_rows()

    if isinstance(plan, Filter):
        return [row for row in _evaluate(plan.source) if _passes(plan.expr, row)]

    if isinstance(plan, InnerJoin):
        left_rows = _evaluate(plan.left)
        right_rows = _evaluate(plan.right)
        out: list[Row] = []
        for lrow in left_rows:
            for rrow in right_rows:
                candidate = _merge(lrow, rrow)
                if _passes(plan.on, candidate):
                    out.append(candidate)
        return out

    if isinstance(plan, LeftOuterJoin):
        left_rows = _evaluate(plan.left)
        right_rows = _evaluate(plan.right)
        right_schema = plan_schema(plan.right)
        out = []
        for lrow in left_rows:
            matched = False
            for rrow in right_rows:
                candidate = _merge(lrow, rrow)
                if _passes(plan.on, candidate):
                    out.append(candidate)
                    matched = True
            if not matched:
                # Preserve the padding row: every right-side column is NULL.
                padded = dict(lrow)
                for key in right_schema:
                    padded[key] = None
                out.append(padded)
        return out

    raise TypeError(f"unknown plan type: {type(plan)!r}")  # pragma: no cover


def evaluate_plan(plan: Plan) -> list[Row]:
    """Evaluate a plan and return its bag (ordered list) of output rows.

    Rows are returned as dictionaries. Use :func:`canonical_bag` to turn the
    result into a hashable multiset for equality comparison in tests.
    """
    return _evaluate(plan)


def canonical_bag(rows: list[Row]) -> dict[frozenset[tuple[tuple[str, str], Any]], int]:
    """Multiset (Counter) representation of an evaluated result, order-free."""
    bag: dict[frozenset[tuple[tuple[str, str], Any]], int] = {}
    for row in rows:
        key = frozenset(row.items())
        bag[key] = bag.get(key, 0) + 1
    return bag


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------

def plan_to_text(plan: Plan, indent: int = 0) -> str:
    """Render a plan as an indented, SQL-like tree (for traces and docs)."""
    pad = "  " * indent
    if isinstance(plan, Scan):
        return f"{pad}Scan {plan.alias}={plan.table.name} [{plan.node_id}]"
    if isinstance(plan, Filter):
        head = f"{pad}Filter [{plan.node_id}] {expr_to_text(plan.expr)}"
        if plan.pids:
            head += f"  <- {', '.join(plan.pids)}"
        return head + "\n" + plan_to_text(plan.source, indent + 1)
    kind = "InnerJoin" if isinstance(plan, InnerJoin) else "LeftOuterJoin"
    head = f"{pad}{kind} [{plan.node_id}] ON {expr_to_text(plan.on)}"
    return (
        head
        + "\n"
        + plan_to_text(plan.left, indent + 1)
        + "\n"
        + plan_to_text(plan.right, indent + 1)
    )
