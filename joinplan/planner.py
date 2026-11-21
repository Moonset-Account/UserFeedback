"""Cost-based left-deep join planner with partition-property propagation.

Search is a dynamic program over ``(joined table set, equivalence classes,
partitioning property)``.  Keeping the partitioning property inside the
state (instead of collapsing to the cheapest plan per table set) is what
lets a slightly more expensive intermediate result that is already
partitioned on the next join key win globally.

Cost model, applied to every candidate step:

* **Broadcast** the right (single) table: ``right_bytes * (nodes - 1)``.
  Allowed only while ``right_bytes <= broadcast_limit`` (when a limit is
  supplied).  The left relation's partitioning property is preserved.
* **Repartition** both sides on the join key: charge the bytes of any side
  that is not already partitioned on the corresponding key.  The result
  inherits the equivalence class of the join key.

Only connected left-deep extensions are generated: the right table must
share at least one join edge with the already-joined left side.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

from .equivalence import EqRelation, class_of, merge, singleton_relation
from .model import InputError, Plan, PlanStep, Query

Partition = frozenset[str] | None


@dataclass(frozen=True)
class _State:
    """Dynamic-programming state."""

    tables: frozenset[str]
    eq: EqRelation
    partition: Partition


@dataclass(frozen=True)
class _Candidate:
    cost: int
    steps: tuple[PlanStep, ...]


@dataclass(frozen=True)
class _StepSpec:
    method: str
    right_table: str
    left_key: str
    right_key: str
    left_transfer: int
    right_transfer: int
    cost: int


def _column_universe(query: Query) -> dict[str, frozenset[str]]:
    """Columns each table exposes: edge endpoints plus its partition key."""
    columns: dict[str, set[str]] = {table.name: set() for table in query.tables}
    for edge in query.edges:
        left_table, left_column = edge.left.split(".")
        right_table, right_column = edge.right.split(".")
        columns[left_table].add(left_column)
        columns[right_table].add(right_column)
    for table in query.tables:
        if table.partition_key is not None:
            columns[table.name].add(table.partition_key)
    return {name: frozenset(values) for name, values in columns.items()}


def _initial_states(
    query: Query, columns: dict[str, frozenset[str]]
) -> dict[_State, _Candidate]:
    states: dict[_State, _Candidate] = {}
    for table in query.tables:
        table_columns = frozenset(
            f"{table.name}.{column}" for column in columns[table.name]
        )
        partition: Partition = None
        if table.partition_key is not None:
            partition = frozenset({f"{table.name}.{table.partition_key}"})
        state = _State(
            tables=frozenset({table.name}),
            eq=singleton_relation(table_columns),
            partition=partition,
        )
        states[state] = _Candidate(cost=0, steps=())
    return states


def _crossing_edges(
    query: Query, joined: frozenset[str], right_table: str
) -> list[tuple[str, str]]:
    """All ``(left_column, right_column)`` edges between the two sides.

    Edge orientation is normalised so the first endpoint is on the joined
    left side and the second endpoint is on ``right_table``.
    """
    crossing: list[tuple[str, str]] = []
    for edge in query.edges:
        left_table, left_column = edge.left.split(".")
        other_table, other_column = edge.right.split(".")
        if left_table in joined and other_table == right_table:
            crossing.append((f"{left_table}.{left_column}", f"{other_table}.{other_column}"))
        elif other_table in joined and left_table == right_table:
            crossing.append((f"{other_table}.{other_column}", f"{left_table}.{left_column}"))
    return crossing


def plan_query(query: Query) -> Plan:
    """Return a minimum-total-transfer left-deep plan for ``query``."""
    if not query.tables:
        raise InputError("at least one table is required")

    columns = _column_universe(query)
    by_size: dict[int, dict[_State, _Candidate]] = {}
    for state, candidate in _initial_states(query, columns).items():
        by_size.setdefault(len(state.tables), {})[state] = candidate

    all_tables = query.table_names
    for size in range(1, len(all_tables)):
        layer = by_size.get(size, {})
        if not layer:
            continue
        next_layer = by_size.setdefault(size + 1, {})
        for state, candidate in layer.items():
            for spec, next_state in _expand(query, columns, state):
                new_cost = candidate.cost + spec.cost
                step = PlanStep(
                    index=len(candidate.steps),
                    method=spec.method,
                    left_tables=state.tables,
                    right_table=spec.right_table,
                    left_key=spec.left_key,
                    right_key=spec.right_key,
                    left_transfer=spec.left_transfer,
                    right_transfer=spec.right_transfer,
                    cost=spec.cost,
                    cumulative_cost=new_cost,
                    result_tables=next_state.tables,
                    result_bytes=query.result_bytes(next_state.tables),
                    result_partition_columns=tuple(sorted(next_state.partition or ())),
                )
                competitor = _Candidate(cost=new_cost, steps=candidate.steps + (step,))
                existing = next_layer.get(next_state)
                if existing is None or _is_better(competitor, existing):
                    next_layer[next_state] = competitor

    finals = [
        (state, candidate)
        for state, candidate in by_size.get(len(all_tables), {}).items()
        if state.tables == all_tables
    ]
    if not finals:
        raise InputError(
            "no left-deep plan joins all tables using only connection edges"
        )
    final_state, best = min(finals, key=lambda item: _plan_sort_key(item[1]))
    return Plan(
        steps=best.steps,
        total_cost=best.cost,
        result_tables=all_tables,
        result_bytes=query.result_bytes(all_tables),
        partition_columns=tuple(sorted(final_state.partition or ())),
    )


def _expand(
    query: Query, columns: dict[str, frozenset[str]], state: _State
) -> Iterator[tuple[_StepSpec, _State]]:
    left_bytes = query.result_bytes(state.tables)
    node_factor = query.nodes - 1

    for table in query.tables:
        if table.name in state.tables:
            continue
        crossing = _crossing_edges(query, state.tables, table.name)
        if not crossing:  # a left-deep extension must follow a join edge
            continue

        # Every predicate between the two sides holds after this join, so
        # every crossing key pair enters the equivalence relation.  The
        # step may still *partition* on just one of those key pairs.
        joined_eq = state.eq
        extra_columns = frozenset(
            f"{table.name}.{column}" for column in columns[table.name]
        )
        for left_key, right_key in crossing:
            joined_eq = merge(joined_eq, left_key, right_key, extra=extra_columns)

        # ---- broadcast the right table ---------------------------------
        # The broadcast is keyed on any crossing predicate; the resulting
        # state (and cost) is identical for all of them, so emit it once.
        if query.broadcast_limit is None or table.nbytes <= query.broadcast_limit:
            broadcast_cost = table.nbytes * node_factor
            broadcast_key = crossing[0]
            # The left partitioning survives physically; its equivalence
            # class grows with the newly proven key equalities, so the
            # result is partitioned on every column of that class.
            broadcast_partition = (
                None
                if state.partition is None
                else class_of(joined_eq, next(iter(state.partition)))
            )
            yield (
                _StepSpec(
                    method="broadcast",
                    right_table=table.name,
                    left_key=broadcast_key[0],
                    right_key=broadcast_key[1],
                    left_transfer=0,
                    right_transfer=broadcast_cost,
                    cost=broadcast_cost,
                ),
                _State(
                    tables=state.tables | {table.name},
                    eq=joined_eq,
                    partition=broadcast_partition,
                ),
            )

        for left_key, right_key in crossing:
            # ---- repartition both sides on the join key ----------------
            left_already = (
                state.partition is not None and left_key in state.partition
            )
            right_already = (
                table.partition_key is not None
                and right_key == f"{table.name}.{table.partition_key}"
            )
            left_transfer = 0 if left_already else left_bytes
            right_transfer = 0 if right_already else table.nbytes
            yield (
                _StepSpec(
                    method="repartition",
                    right_table=table.name,
                    left_key=left_key,
                    right_key=right_key,
                    left_transfer=left_transfer,
                    right_transfer=right_transfer,
                    cost=left_transfer + right_transfer,
                ),
                _State(
                    tables=state.tables | {table.name},
                    eq=joined_eq,
                    partition=class_of(joined_eq, left_key),
                ),
            )


def _plan_sort_key(candidate: _Candidate) -> tuple:
    """Deterministic tie-break so equivalent optima resolve consistently."""
    return (
        candidate.cost,
        tuple(
            (step.method, step.right_table, step.left_key, step.right_key)
            for step in candidate.steps
        ),
    )


def _is_better(candidate: _Candidate, existing: _Candidate) -> bool:
    return _plan_sort_key(candidate) < _plan_sort_key(existing)
