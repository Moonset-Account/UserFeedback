"""Shared helpers for the MERGE test suite."""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from merge_store import (  # noqa: E402  (path set up above)
    Change,
    Conflict,
    Match,
    MergeConfigError,
    MergeConflictError,
    MergeError,
    MergeOutcome,
    MergeStatement,
    RelationStore,
    Rule,
    TargetRow,
    TargetTable,
    default_insert_row_id,
    execute_merge,
)

__all__ = [
    "Change",
    "Conflict",
    "Match",
    "MergeConfigError",
    "MergeConflictError",
    "MergeError",
    "MergeOutcome",
    "MergeStatement",
    "RelationStore",
    "Rule",
    "TargetRow",
    "TargetTable",
    "default_insert_row_id",
    "execute_merge",
]


def table(*rows):
    """Build a TargetTable from ``(row_id, values)`` tuples."""
    return TargetTable(tuple(rows))


def values_by_id(outcome):
    return outcome.target.as_dict()
