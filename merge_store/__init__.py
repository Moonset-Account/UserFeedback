"""In-memory MERGE execution for relation stores.

Public API
----------
``execute_merge(source_rows, target, match_columns, rules, ...)``
    One-shot execution returning a :class:`~merge_store.model.MergeOutcome`.
``MergeStatement(match_columns, rules).execute(target, source_rows)``
    Reusable statement object.
``RelationStore(table).merge(source_rows, match_columns, rules)``
    Stateful wrapper that commits atomically.

Data types: :class:`TargetRow`, :class:`TargetTable`, :class:`Rule`,
:class:`Match`, :class:`Change`, :class:`Conflict`, :class:`MergeOutcome`.
Errors: :class:`MergeConfigError` (bad plan/input),
:class:`MergeConflictError` (opt-in raise carrying the outcome).
"""

from .engine import MergeStatement, RelationStore, default_insert_row_id, execute_merge
from .model import (
    Change,
    Conflict,
    Match,
    MergeConfigError,
    MergeConflictError,
    MergeError,
    MergeOutcome,
    Rule,
    TargetRow,
    TargetTable,
)

__version__ = "1.0.0"

__all__ = [
    "execute_merge",
    "MergeStatement",
    "RelationStore",
    "default_insert_row_id",
    "TargetRow",
    "TargetTable",
    "Rule",
    "Match",
    "Change",
    "Conflict",
    "MergeOutcome",
    "MergeError",
    "MergeConfigError",
    "MergeConflictError",
    "__version__",
]
