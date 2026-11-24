"""Independent brute-force oracle for testing the repair engine.

Nothing here imports the engine's graph, solver or derivation code.  The oracle
re-derives constraints directly from the :mod:`consistent_query.model`
declarations, enumerates every subset of rows, and computes the answer
classification from first principles.  It is deliberately exponential: it is a
verification tool for small instances, not a solver.

Two independent ideas are checked:

* constraints are re-evaluated on each candidate survivor set from scratch, and
* the three-way answer status is recomputed by testing, for every optimal
  repair, whether *any* derivation survives it.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations, product
from typing import Iterable, Sequence

from .model import Problem, Query, Row, RowKey


@dataclass(frozen=True)
class OracleReport:
    optimal_cost: int
    optimal_repairs: tuple[frozenset[RowKey], ...]
    answers: dict[tuple, str]
    derivation_count: dict[tuple, int]


def _all_rows(problem: Problem) -> list[tuple[RowKey, Row]]:
    result: list[tuple[RowKey, Row]] = []
    for table in problem.tables:
        for row in table.rows:
            result.append((RowKey(table.name, row.rid), row))
    return result


def _survivors_valid(
    problem: Problem,
    survivors: frozenset[RowKey],
    values: dict[RowKey, Row],
) -> bool:
    # Unique keys: two surviving rows of a table may not share a key value.
    for table in problem.tables:
        alive = [values[RowKey(table.name, row.rid)] for row in table.rows
                 if RowKey(table.name, row.rid) in survivors]
        for key_columns in table.keys:
            seen = set()
            for row in alive:
                marker = tuple(row.values[column] for column in key_columns)
                if marker in seen:
                    return False
                seen.add(marker)

    # Foreign keys.
    by_name = {table.name: table for table in problem.tables}
    for fk in problem.foreign_keys:
        parent_table = by_name[fk.parent_table]
        child_table = by_name[fk.child_table]
        parent_keys = set()
        for row in parent_table.rows:
            key = RowKey(parent_table.name, row.rid)
            if key in survivors:
                parent_keys.add(
                    tuple(row.values[column] for column in fk.parent_columns)
                )
        for row in child_table.rows:
            key = RowKey(child_table.name, row.rid)
            if key not in survivors:
                continue
            child_value = tuple(row.values[column] for column in fk.child_columns)
            if any(value is None for value in child_value):
                if fk.mandatory:
                    return False
                continue
            if child_value not in parent_keys:
                return False

    # Denials.
    for denial in problem.denials:
        left_table = by_name[denial.left_table]
        right_table = by_name[denial.right_table]
        for left_row in left_table.rows:
            left_key = RowKey(left_table.name, left_row.rid)
            if left_key not in survivors:
                continue
            for right_row in right_table.rows:
                right_key = RowKey(right_table.name, right_row.rid)
                if right_key not in survivors:
                    continue
                if left_key == right_key:
                    continue
                if all(
                    left_row.values[left_column] == right_row.values[right_column]
                    for left_column, right_column in zip(
                        denial.left_columns, denial.right_columns
                    )
                ):
                    return False
    return True


def _all_derivations(problem: Problem, query: Query) -> list[tuple[RowKey, ...]]:
    by_name = {table.name: table for table in problem.tables}
    row_lists = [list(by_name[name].rows) for name in query.tables]
    result: list[tuple[RowKey, ...]] = []

    def values_of(choice: Sequence[Row], ref: tuple[str, str]):
        position = query.tables.index(ref[0])
        return choice[position].values[ref[1]]

    for choice in product(*row_lists):
        ok = True
        for left, right in query.equalities:
            left_value = values_of(choice, left)
            right_value = values_of(choice, right)
            if left_value is None or right_value is None or left_value != right_value:
                ok = False
                break
        if not ok:
            continue
        result.append(
            tuple(RowKey(query.tables[i], choice[i].rid) for i in range(len(choice)))
        )
    return result


def _projection(problem: Problem, query: Query, derivation: Sequence[RowKey]) -> tuple:
    by_name = {table.name: table for table in problem.tables}
    rows = {}
    for key in derivation:
        table = by_name[key.table]
        for row in table.rows:
            if row.rid == key.rid:
                rows[key.table] = row
                break
        else:  # pragma: no cover
            raise AssertionError("unknown row %s" % (key,))
    return tuple(rows[table].values[column] for table, column in query.projection)


def is_valid_repair(
    problem: Problem,
    survivors: Iterable[RowKey],
    pins: Iterable[tuple[RowKey, bool]] = (),
) -> bool:
    """Re-check one candidate survivor set directly against the declarations."""

    survivor_set = frozenset(survivors)
    known = set(problem.row_keys())
    if not survivor_set <= known:
        return False
    values = dict(_all_rows(problem))
    for key, keep in pins:
        if keep != (key in survivor_set):
            return False
    return _survivors_valid(problem, survivor_set, values)


def repair_cost(problem: Problem, survivors: Iterable[RowKey]) -> int:
    """Cost of deleting every row not in ``survivors``."""

    survivor_set = frozenset(survivors)
    return sum(
        row.cost
        for table in problem.tables
        for row in table.rows
        if RowKey(table.name, row.rid) not in survivor_set
    )


def oracle(
    problem: Problem,
    query: Query,
    pins: Iterable[tuple[RowKey, bool]] = (),
) -> OracleReport:
    """Brute-force every survivor set and classify answers independently."""

    pin_mode: dict[RowKey, bool] = {}
    for key, keep in pins:
        pin_mode[key] = keep

    rows = _all_rows(problem)
    values = dict(rows)
    keys = [key for key, _ in rows]
    costs = {key: values[key].cost for key in keys}

    survivors_by_cost: dict[int, list[frozenset[RowKey]]] = {}
    for size in range(len(keys) + 1):
        for deleted in combinations(keys, size):
            deleted_set = set(deleted)
            survivors = frozenset(key for key in keys if key not in deleted_set)
            skip = False
            for key, keep in pin_mode.items():
                if keep and key not in survivors:
                    skip = True
                    break
                if not keep and key in survivors:
                    skip = True
                    break
            if skip:
                continue
            if not _survivors_valid(problem, survivors, values):
                continue
            cost = sum(costs[key] for key in deleted)
            survivors_by_cost.setdefault(cost, []).append(survivors)

    if not survivors_by_cost:
        raise RuntimeError("oracle: instance has no valid repair")
    best_cost = min(survivors_by_cost)
    optimal = tuple(sorted(survivors_by_cost[best_cost], key=lambda s: sorted(map(str, s))))

    derivations = _all_derivations(problem, query)
    answers: dict[tuple, str] = {}
    counts: dict[tuple, int] = {}
    grouped: dict[tuple, list[tuple[RowKey, ...]]] = {}
    for derivation in derivations:
        projection = _projection(problem, query, derivation)
        grouped.setdefault(projection, []).append(derivation)
    for projection, derivs in grouped.items():
        counts[projection] = len(derivs)
        supports_any = False
        supports_all = True
        for repair in optimal:
            derivable = any(all(key in repair for key in d) for d in derivs)
            supports_any = supports_any or derivable
            supports_all = supports_all and derivable
        if supports_all:
            answers[projection] = "certain"
        elif supports_any:
            answers[projection] = "contingent"
        else:
            answers[projection] = "absent"

    return OracleReport(
        optimal_cost=best_cost,
        optimal_repairs=optimal,
        answers=answers,
        derivation_count=counts,
    )


__all__ = ["OracleReport", "is_valid_repair", "oracle", "repair_cost"]
