"""Predicate rewrite module for inner / left-outer join query plans.

Public modules:

* :mod:`predrewrite.expressions` -- expression tree, three-valued logic and
  the symbolic "null-rejection" proof used to justify outer-to-inner
  conversion.
* :mod:`predrewrite.plans` -- logical plan node definitions and a bag
  (multiset) evaluator implementing SQL three-valued logic.
* :mod:`predrewrite.rewrite` -- the predicate pushdown / outer-to-inner
  rewrite engine together with a trace of where every predicate went.

The implementation only depends on the Python standard library.
"""

from .expressions import (
    And,
    Column,
    Compare,
    Expr,
    IsNull,
    Literal,
    Not,
    Or,
    eval_expr,
    is_null_rejecting,
    split_conjuncts,
    and_exprs,
    referenced_aliases,
)
from .plans import (
    Filter,
    InnerJoin,
    LeftOuterJoin,
    Plan,
    Scan,
    evaluate_plan,
    plan_schema,
    plan_to_text,
)
from .rewrite import (
    ConversionEvent,
    PredicateEvent,
    RewriteResult,
    rewrite,
)

__all__ = [
    "And",
    "Column",
    "Compare",
    "Expr",
    "IsNull",
    "Literal",
    "Not",
    "Or",
    "eval_expr",
    "is_null_rejecting",
    "split_conjuncts",
    "and_exprs",
    "referenced_aliases",
    "Filter",
    "InnerJoin",
    "LeftOuterJoin",
    "Plan",
    "Scan",
    "evaluate_plan",
    "plan_schema",
    "plan_to_text",
    "ConversionEvent",
    "PredicateEvent",
    "RewriteResult",
    "rewrite",
]
