"""Exact minimum-cost deletion repairs and consistent query answering.

Pipeline
--------
1. Rows are linked by the constraints they share and split into independent
   *components*.  A repair is valid exactly when its restriction to every
   component is valid, and costs add up, so the optimum decomposes.
2. Each component is solved by an exhaustive cost-pruned search that returns
   *all* minimum-cost survivor sets for that component.
3. Query derivations (one stored row per queried table satisfying all join
   equalities) are enumerated.  A derivation survives a repair when all of its
   rows survive.

A projection tuple is

* ``certain``    -- derived in *every* optimal repair,
* ``contingent`` -- derived in some but not all optimal repairs,
* ``absent``     -- derived in no optimal repair.

Certainty is decided over the Cartesian product of component repairs, tracking
the set of derivations still alive.  This matters because a tuple may be
certain only because different optimal repairs use different derivations (the
bundled demo shows exactly that).  :mod:`consistent_query.verify` is a separate
brute-force oracle that shares no solving code with this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from .model import ModelError, Problem, Query, Row, RowKey, coerce_row_key

STATUS_CERTAIN = "certain"
STATUS_CONTINGENT = "contingent"
STATUS_ABSENT = "absent"


class RepairError(RuntimeError):
    """Base class for repair-engine failures."""


class RepairLimitExceeded(RepairError):
    """The instance is too large for an exact run under the configured limits."""


class InfeasiblePins(RepairError):
    """No valid repair respects the requested fixed facts."""


@dataclass(frozen=True)
class Limits:
    """Guard rails that keep the exhaustive engine honest about its scope."""

    max_component_rows: int = 20
    max_repairs_per_component: int = 20000
    max_derivations: int = 200000
    max_certainty_nodes: int = 500000

    def __post_init__(self) -> None:
        for name in (
            "max_component_rows",
            "max_repairs_per_component",
            "max_derivations",
            "max_certainty_nodes",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ModelError("%s must be a positive integer" % name)


@dataclass(frozen=True)
class Pin:
    """A fixed fact: a row that must survive (``keep``) or be deleted."""

    row: RowKey
    keep: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "row", coerce_row_key(self.row))
        if not isinstance(self.keep, bool):
            raise ModelError("Pin.keep must be a bool")


def keep_row(row: Any) -> Pin:
    """Pin a row so that it survives in every considered repair."""

    return Pin(coerce_row_key(row), True)


def delete_row(row: Any) -> Pin:
    """Pin a row so that it is deleted in every considered repair."""

    return Pin(coerce_row_key(row), False)


def _normalise_pins(pins: Iterable[Any], problem: Problem) -> tuple[Pin, ...]:
    known = set(problem.row_keys())
    normalised: list[Pin] = []
    seen: dict[RowKey, bool] = {}
    for raw in pins:
        if isinstance(raw, Pin):
            pin = raw
        else:
            try:
                row, keep = raw
            except (TypeError, ValueError):
                raise ModelError(
                    "pins must be Pin instances or (row, keep) pairs"
                ) from None
            if isinstance(keep, str):
                lowered = keep.lower()
                if lowered not in ("keep", "delete"):
                    raise ModelError("pin mode must be 'keep' or 'delete'")
                keep = lowered == "keep"
            pin = Pin(coerce_row_key(row), bool(keep))
        if pin.row not in known:
            raise ModelError("pin references unknown row %s" % (pin.row,))
        if pin.row in seen and seen[pin.row] != pin.keep:
            raise ModelError("row %s is pinned both alive and deleted" % (pin.row,))
        seen[pin.row] = pin.keep
        if pin not in normalised:
            normalised.append(pin)
    return tuple(normalised)


@dataclass(frozen=True)
class Witness:
    """An optimal repair and, optionally, a derivation it supports.

    ``derivation`` is populated for a support witness (the derivation is fully
    contained in ``repair``).  It is ``None`` for an exclusion witness, where
    ``repair`` is an optimal repair whose answer lacks the tuple.
    """

    repair: frozenset[RowKey]
    derivation: tuple[RowKey, ...] | None = None


@dataclass(frozen=True)
class CandidateVerdict:
    answer: tuple
    status: str
    derivation_count: int
    viable_derivation_count: int
    support: Witness | None
    exclusion: Witness | None
    sample_derivations: tuple[tuple[RowKey, ...], ...] = ()

    @property
    def certain(self) -> bool:
        return self.status == STATUS_CERTAIN

    @property
    def possible(self) -> bool:
        return self.status != STATUS_ABSENT


@dataclass(frozen=True)
class ComponentReport:
    rows: frozenset[RowKey]
    cost: int
    optimal_repairs: tuple[frozenset[RowKey], ...]
    required_rows: frozenset[RowKey]
    possible_rows: frozenset[RowKey]

    @property
    def optimal_repair_count(self) -> int:
        return len(self.optimal_repairs)

    @property
    def sample_repairs(self) -> tuple[frozenset[RowKey], ...]:
        return self.optimal_repairs[:3]


@dataclass(frozen=True)
class Derivation:
    """One stored row per queried table satisfying the join equalities."""

    keys: tuple[RowKey, ...]
    projection: tuple

    @property
    def tables(self) -> tuple[str, ...]:
        return tuple(key.table for key in self.keys)


@dataclass(frozen=True)
class AnalysisReport:
    optimal_cost: int
    optimal_repair_count: int
    components: tuple[ComponentReport, ...]
    candidates: tuple[CandidateVerdict, ...]
    derivable_answers: tuple[tuple, ...]
    pins: tuple[Pin, ...] = ()

    @property
    def certain_answers(self) -> tuple[tuple, ...]:
        return tuple(c.answer for c in self.candidates if c.status == STATUS_CERTAIN)

    @property
    def contingent_answers(self) -> tuple[tuple, ...]:
        return tuple(
            c.answer for c in self.candidates if c.status == STATUS_CONTINGENT
        )

    @property
    def absent_answers(self) -> tuple[tuple, ...]:
        return tuple(c.answer for c in self.candidates if c.status == STATUS_ABSENT)

    def verdict(self, answer: Sequence[Any]) -> CandidateVerdict:
        wanted = tuple(answer)
        for candidate in self.candidates:
            if candidate.answer == wanted:
                return candidate
        raise KeyError("answer %r is not derivable from the input" % (wanted,))

    @property
    def required_rows(self) -> frozenset[RowKey]:
        """Rows surviving in *every* minimum-cost repair."""

        result: set[RowKey] = set()
        for component in self.components:
            result.update(component.required_rows)
        return frozenset(result)

    @property
    def possible_rows(self) -> frozenset[RowKey]:
        """Rows surviving in at least one minimum-cost repair."""

        result: set[RowKey] = set()
        for component in self.components:
            result.update(component.possible_rows)
        return frozenset(result)

    def row_status(self, key: Any) -> str:
        """``'required'``, ``'possible'`` or ``'never'`` for one stored row."""

        row = key if isinstance(key, RowKey) else coerce_row_key(key)
        if row in self.required_rows:
            return "required"
        if row in self.possible_rows:
            return "possible"
        return "never"


# ---------------------------------------------------------------------------
# Constraint graph


@dataclass
class _Graph:
    keys: list[RowKey]
    index_of: dict[RowKey, int]
    parent: list[int]
    conflicts: list[list[int]]
    implications: list[tuple[int, tuple[int, ...]]]

    def find(self, item: int) -> int:
        root = item
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[item] != item:
            nxt = self.parent[item]
            self.parent[item] = root
            item = nxt
        return root

    def union(self, left: int, right: int) -> None:
        lroot, rroot = self.find(left), self.find(right)
        if lroot != rroot:
            self.parent[max(lroot, rroot)] = min(lroot, rroot)


def _build_graph(problem: Problem) -> _Graph:
    keys: list[RowKey] = []
    index_of: dict[RowKey, int] = {}
    for table in problem.tables:
        for row in table.rows:
            key = RowKey(table.name, row.rid)
            index_of[key] = len(keys)
            keys.append(key)

    graph = _Graph(
        keys=keys,
        index_of=index_of,
        parent=list(range(len(keys))),
        conflicts=[],
        implications=[],
    )

    for table in problem.tables:
        row_indices = [index_of[RowKey(table.name, row.rid)] for row in table.rows]
        for key_columns in table.keys:
            buckets: dict[tuple, list[int]] = {}
            for index, row in zip(row_indices, table.rows):
                bucket = tuple(row.values[column] for column in key_columns)
                buckets.setdefault(bucket, []).append(index)
            for bucket in buckets.values():
                if len(bucket) > 1:
                    graph.conflicts.append(list(bucket))
                    for other in bucket[1:]:
                        graph.union(bucket[0], other)

    for fk in problem.foreign_keys:
        parent_table = problem.table(fk.parent_table)
        child_table = problem.table(fk.child_table)
        parent_buckets: dict[tuple, list[int]] = {}
        for row in parent_table.rows:
            index = index_of[RowKey(parent_table.name, row.rid)]
            bucket = tuple(row.values[column] for column in fk.parent_columns)
            parent_buckets.setdefault(bucket, []).append(index)
        for row in child_table.rows:
            child_index = index_of[RowKey(child_table.name, row.rid)]
            values = tuple(row.values[column] for column in fk.child_columns)
            if any(value is None for value in values):
                if fk.mandatory:
                    graph.implications.append((child_index, ()))
                continue
            parents = tuple(parent_buckets.get(values, ()))
            graph.implications.append((child_index, parents))
            for parent_index in parents:
                graph.union(child_index, parent_index)

    for denial in problem.denials:
        left_table = problem.table(denial.left_table)
        right_table = problem.table(denial.right_table)
        for left_row in left_table.rows:
            left_index = index_of[RowKey(left_table.name, left_row.rid)]
            for right_row in right_table.rows:
                right_index = index_of[RowKey(right_table.name, right_row.rid)]
                if left_index == right_index:
                    continue
                if all(
                    left_row.values[left_column] == right_row.values[right_column]
                    for left_column, right_column in zip(
                        denial.left_columns, denial.right_columns
                    )
                ):
                    graph.conflicts.append([left_index, right_index])
                    graph.union(left_index, right_index)

    seen_conflicts: set[tuple[int, ...]] = set()
    unique_conflicts: list[list[int]] = []
    for conflict in graph.conflicts:
        marker = tuple(sorted(conflict))
        if marker not in seen_conflicts:
            seen_conflicts.add(marker)
            unique_conflicts.append(list(conflict))
    graph.conflicts = unique_conflicts

    seen_implications: set[tuple[int, tuple[int, ...]]] = set()
    unique_implications: list[tuple[int, tuple[int, ...]]] = []
    for implication in graph.implications:
        if implication not in seen_implications:
            seen_implications.add(implication)
            unique_implications.append(implication)
    graph.implications = unique_implications
    return graph


def _components(graph: _Graph) -> list[list[int]]:
    buckets: dict[int, list[int]] = {}
    for index in range(len(graph.keys)):
        buckets.setdefault(graph.find(index), []).append(index)
    return [buckets[root] for root in sorted(buckets)]


# ---------------------------------------------------------------------------
# One component: all minimum-cost survivor sets


def _solve_component(
    row_keys: Sequence[RowKey],
    costs: Sequence[int],
    conflicts: Sequence[Sequence[int]],
    implications: Sequence[tuple[int, Sequence[int]]],
    pinned_keep: frozenset[int],
    pinned_delete: frozenset[int],
    limits: Limits,
) -> tuple[int, list[frozenset[RowKey]]]:
    n = len(row_keys)
    if n > limits.max_component_rows:
        raise RepairLimitExceeded(
            "component with %d rows exceeds max_component_rows=%d"
            % (n, limits.max_component_rows)
        )

    groups_of: list[list[int]] = [[] for _ in range(n)]
    groups: list[tuple[int, ...]] = []
    for conflict in conflicts:
        group_id = len(groups)
        groups.append(tuple(conflict))
        for index in conflict:
            groups_of[index].append(group_id)

    # A row may be the child of several foreign keys; each key is a separate
    # obligation, so store the parent groups *per implication*, never merged.
    obligations: list[tuple[int, tuple[int, ...]]] = [
        (child, tuple(parents)) for child, parents in implications
    ]
    obligations_of_child: list[list[int]] = [[] for _ in range(n)]
    for obligation_id, (child, _parents) in enumerate(obligations):
        obligations_of_child[child].append(obligation_id)
    obligations_of_parent: list[list[int]] = [[] for _ in range(n)]
    for obligation_id, (_child, parents) in enumerate(obligations):
        for parent in parents:
            obligations_of_parent[parent].append(obligation_id)

    keep = [False] * n
    decided = [False] * n
    kept_in_group = [0] * len(groups)

    # A row that takes part in no constraint is always kept in every optimum:
    # keeping it is free and cannot violate anything, while deleting it only
    # adds cost.  Pinning it out of the search avoids 2**n blow-ups on tables
    # with no keys and no references.
    free = [
        not groups_of[index]
        and not obligations_of_child[index]
        and not obligations_of_parent[index]
        for index in range(n)
    ]

    def parent_possible(parent: int) -> bool:
        if decided[parent]:
            return keep[parent]
        return parent not in pinned_delete

    def can_keep(index: int) -> bool:
        if index in pinned_delete:
            return False
        for group_id in groups_of[index]:
            if kept_in_group[group_id] > 0:
                return False
        for obligation_id in obligations_of_child[index]:
            _child, parents = obligations[obligation_id]
            satisfied = False
            for parent in parents:
                if parent == index or parent_possible(parent):
                    satisfied = True
                    break
            if not satisfied:
                return False
        return True

    def can_delete(index: int) -> bool:
        if index in pinned_keep:
            return False
        for obligation_id in obligations_of_parent[index]:
            child, parents = obligations[obligation_id]
            if not (decided[child] and keep[child]):
                continue
            supported = False
            for parent in parents:
                if parent == index:
                    continue
                if parent_possible(parent):
                    supported = True
                    break
            if not supported:
                return False
        return True

    def leaf_valid() -> bool:
        for child, parents in implications:
            if keep[child]:
                if not any(keep[parent] for parent in parents):
                    return False
        return True

    best: int | None = None
    solutions: list[frozenset[RowKey]] = []

    def search(index: int, cost_so_far: int) -> None:
        nonlocal best, solutions
        if best is not None and cost_so_far > best:
            return
        if index == n:
            if leaf_valid():
                survivor = frozenset(row_keys[i] for i in range(n) if keep[i])
                if best is None or cost_so_far < best:
                    best = cost_so_far
                    solutions = [survivor]
                elif cost_so_far == best:
                    solutions.append(survivor)
                    if len(solutions) > limits.max_repairs_per_component:
                        raise RepairLimitExceeded(
                            "component has more than %d optimal repairs"
                            % (limits.max_repairs_per_component,)
                        )
            return

        if free[index] and index not in pinned_delete:
            # Forced keep: no constraint can be violated and deleting it is
            # pure cost, so no branching is needed.
            keep[index] = True
            decided[index] = True
            search(index + 1, cost_so_far)
            decided[index] = False
            keep[index] = False
            return

        if can_keep(index):
            keep[index] = True
            decided[index] = True
            touched = groups_of[index]
            for group_id in touched:
                kept_in_group[group_id] += 1
            search(index + 1, cost_so_far)
            for group_id in touched:
                kept_in_group[group_id] -= 1
            decided[index] = False
            keep[index] = False

        if can_delete(index):
            decided[index] = True
            search(index + 1, cost_so_far + costs[index])
            decided[index] = False

    search(0, 0)
    if best is None:
        raise InfeasiblePins(
            "no repair of component %s respects the pinned facts"
            % (sorted(str(key) for key in row_keys),)
        )
    return best, solutions


# ---------------------------------------------------------------------------
# Query derivations


def _validate_query(problem: Problem, query: Query) -> None:
    by_name = {table.name: table for table in problem.tables}
    for name in query.tables:
        if name not in by_name:
            raise ModelError("query names unknown table %r" % (name,))
    refs = list(query.projection)
    for pair in query.equalities:
        refs.extend(pair)
    for table, column in refs:
        if table not in by_name:
            raise ModelError("query references unknown table %r" % (table,))
        if column not in by_name[table].columns:
            raise ModelError(
                "query references unknown column %r of table %r" % (column, table)
            )
    for (left_table, _), (right_table, _) in query.equalities:
        if left_table not in query.tables or right_table not in query.tables:
            raise ModelError("query equality references a table not in the join")
    for table, _ in query.projection:
        if table not in query.tables:
            raise ModelError("query projection references a table not in the join")


def _enumerate_derivations(
    problem: Problem, query: Query, limits: Limits
) -> tuple[dict[tuple, list[Derivation]], int]:
    by_name = {table.name: table for table in problem.tables}
    rows_by_table = [list(by_name[name].rows) for name in query.tables]
    positions = {name: index for index, name in enumerate(query.tables)}
    equalities = [
        (positions[left_table], left_column, positions[right_table], right_column)
        for (left_table, left_column), (right_table, right_column) in query.equalities
    ]
    chosen: list[Row | None] = [None] * len(query.tables)
    grouped: dict[tuple, list[Derivation]] = {}
    total = 0

    def value_at(ref: tuple[str, str]) -> Any:
        table, column = ref
        row = chosen[positions[table]]
        if row is None:  # pragma: no cover - guarded by the caller
            raise AssertionError("equality read before its row was chosen")
        return row.values[column]

    def joins_so_far(position: int) -> bool:
        for left_pos, left_col, right_pos, right_col in equalities:
            if left_pos > position or right_pos > position:
                continue
            left_value = chosen[left_pos].values[left_col]  # type: ignore[union-attr]
            right_value = chosen[right_pos].values[right_col]  # type: ignore[union-attr]
            if left_value is None or right_value is None or left_value != right_value:
                return False
        return True

    keys_in_order = query.tables

    def recurse(position: int) -> None:
        nonlocal total
        if position == len(keys_in_order):
            projection = tuple(value_at(ref) for ref in query.projection)
            key_tuple = tuple(
                RowKey(keys_in_order[i], chosen[i].rid)  # type: ignore[union-attr]
                for i in range(len(keys_in_order))
            )
            grouped.setdefault(projection, []).append(
                Derivation(keys=key_tuple, projection=projection)
            )
            total += 1
            if total > limits.max_derivations:
                raise RepairLimitExceeded(
                    "query has more than %d derivations" % (limits.max_derivations,)
                )
            return
        for candidate in rows_by_table[position]:
            chosen[position] = candidate
            if joins_so_far(position):
                recurse(position + 1)
        chosen[position] = None

    recurse(0)
    return grouped, total


# ---------------------------------------------------------------------------
# Analysis


def _answer_sort_key(answer: tuple) -> tuple:
    return tuple(
        (value is not None, "" if value is None else repr(value)) for value in answer
    )


def analyse(
    problem: Problem,
    query: Query,
    pins: Iterable[Any] = (),
    limits: Limits | None = None,
) -> AnalysisReport:
    """Classify every derivable query answer over all minimum-cost repairs."""

    limits = limits or Limits()
    _validate_query(problem, query)
    normalised_pins = _normalise_pins(pins, problem)
    pin_mode = {pin.row: pin.keep for pin in normalised_pins}

    graph = _build_graph(problem)
    components = _components(graph)

    component_reports: list[ComponentReport] = []
    component_repairs: list[list[frozenset[RowKey]]] = []
    component_of: dict[RowKey, int] = {}

    for component_id, indices in enumerate(components):
        index_set = set(indices)
        local = {global_index: pos for pos, global_index in enumerate(indices)}
        row_keys = [graph.keys[global_index] for global_index in indices]
        for key in row_keys:
            component_of[key] = component_id

        costs = [problem.row(key).cost for key in row_keys]
        conflicts = [
            [local[item] for item in conflict]
            for conflict in graph.conflicts
            if set(conflict) <= index_set
        ]
        implications = [
            (local[child], [local[parent] for parent in parents])
            for child, parents in graph.implications
            if child in index_set
        ]

        pinned_keep = frozenset(
            local[graph.index_of[key]] for key in row_keys if pin_mode.get(key) is True
        )
        pinned_delete = frozenset(
            local[graph.index_of[key]] for key in row_keys if pin_mode.get(key) is False
        )

        cost, solutions = _solve_component(
            row_keys,
            costs,
            conflicts,
            implications,
            pinned_keep,
            pinned_delete,
            limits,
        )
        component_repairs.append(solutions)
        required = frozenset.intersection(*solutions) if solutions else frozenset()
        possible = frozenset().union(*solutions)
        component_reports.append(
            ComponentReport(
                rows=frozenset(row_keys),
                cost=cost,
                optimal_repairs=tuple(solutions),
                required_rows=required,
                possible_rows=possible,
            )
        )

    optimal_cost = sum(report.cost for report in component_reports)
    optimal_repair_count = 1
    for report in component_reports:
        optimal_repair_count *= report.optimal_repair_count

    grouped, _ = _enumerate_derivations(problem, query, limits)
    derivable_answers = tuple(sorted(grouped, key=_answer_sort_key))

    verdicts = tuple(
        _classify(grouped[answer], answer, component_of, component_repairs, limits)
        for answer in derivable_answers
    )

    return AnalysisReport(
        optimal_cost=optimal_cost,
        optimal_repair_count=optimal_repair_count,
        components=tuple(component_reports),
        candidates=verdicts,
        derivable_answers=derivable_answers,
        pins=normalised_pins,
    )


def _classify(
    derivations: Sequence[Derivation],
    answer: tuple,
    component_of: dict[RowKey, int],
    component_repairs: Sequence[Sequence[frozenset[RowKey]]],
    limits: Limits,
) -> CandidateVerdict:
    fragments: list[dict[int, frozenset[RowKey]]] = []
    for derivation in derivations:
        per_component: dict[int, set[RowKey]] = {}
        for key in derivation.keys:
            per_component.setdefault(component_of[key], set()).add(key)
        fragments.append(
            {component_id: frozenset(rows) for component_id, rows in per_component.items()}
        )
    touched = sorted({c for fragment in fragments for c in fragment})

    contain_cache: dict[tuple[int, frozenset[RowKey]], frozenset[int]] = {}

    def repairs_containing(component_id: int, rows: frozenset[RowKey]) -> frozenset[int]:
        cache_key = (component_id, rows)
        cached = contain_cache.get(cache_key)
        if cached is not None:
            return cached
        repairs = component_repairs[component_id]
        if not rows:
            result = frozenset(range(len(repairs)))
        else:
            current: set[int] | None = None
            for row_index, repair in enumerate(repairs):
                if rows <= repair:
                    if current is None:
                        current = set()
                    current.add(row_index)
            result = frozenset(current or ())
        contain_cache[cache_key] = result
        return result

    viable = [
        derivation_id
        for derivation_id, fragment in enumerate(fragments)
        if all(
            repairs_containing(component_id, rows)
            for component_id, rows in fragment.items()
        )
    ]

    if not viable:
        return CandidateVerdict(
            answer=answer,
            status=STATUS_ABSENT,
            derivation_count=len(derivations),
            viable_derivation_count=0,
            support=None,
            exclusion=Witness(repair=_first_global_repair(component_repairs)),
            sample_derivations=tuple(d.keys for d in derivations[:3]),
        )

    support = _support_witness(viable[0], fragments, component_repairs, derivations)
    exclusion = _find_exclusion(
        viable, fragments, touched, component_repairs, limits
    )
    status = STATUS_CERTAIN if exclusion is None else STATUS_CONTINGENT
    return CandidateVerdict(
        answer=answer,
        status=status,
        derivation_count=len(derivations),
        viable_derivation_count=len(viable),
        support=support,
        exclusion=exclusion,
        sample_derivations=tuple(derivations[i].keys for i in viable[:3]),
    )


def _first_global_repair(
    component_repairs: Sequence[Sequence[frozenset[RowKey]]],
) -> frozenset[RowKey]:
    result: set[RowKey] = set()
    for repairs in component_repairs:
        result.update(repairs[0])
    return frozenset(result)


def _support_witness(
    derivation_id: int,
    fragments: Sequence[dict[int, frozenset[RowKey]]],
    component_repairs: Sequence[Sequence[frozenset[RowKey]]],
    derivations: Sequence[Derivation],
) -> Witness:
    chosen: set[RowKey] = set()
    fragment = fragments[derivation_id]
    for component_id, repairs in enumerate(component_repairs):
        rows = fragment.get(component_id, frozenset())
        if not rows:
            chosen.update(repairs[0])
            continue
        for repair in repairs:
            if rows <= repair:
                chosen.update(repair)
                break
        else:  # pragma: no cover - guaranteed by viability
            raise AssertionError("viable derivation has no containing repair")
    return Witness(
        repair=frozenset(chosen), derivation=derivations[derivation_id].keys
    )


def _repair_alive_masks(
    component_id: int,
    viable: Sequence[int],
    fragments: Sequence[dict[int, frozenset[RowKey]]],
    component_repairs: Sequence[Sequence[frozenset[RowKey]]],
) -> list[tuple[int, frozenset[RowKey]]]:
    """Distinct (alive-mask, representative repair) pairs for one component."""

    repairs = component_repairs[component_id]
    seen: dict[int, frozenset[RowKey]] = {}
    for repair in repairs:
        mask = 0
        for position, derivation_id in enumerate(viable):
            rows = fragments[derivation_id].get(component_id, frozenset())
            if not rows or rows <= repair:
                mask |= 1 << position
        seen.setdefault(mask, repair)
    return sorted(seen.items(), key=lambda item: -bin(item[0]).count("1"))


def _find_exclusion(
    viable: Sequence[int],
    fragments: Sequence[dict[int, frozenset[RowKey]]],
    touched: Sequence[int],
    component_repairs: Sequence[Sequence[frozenset[RowKey]]],
    limits: Limits,
) -> Witness | None:
    """Return an optimal repair deriving no viable derivation, if one exists."""

    if not touched:
        return None

    masks_by_component = {
        component_id: _repair_alive_masks(
            component_id, viable, fragments, component_repairs
        )
        for component_id in touched
    }
    order = sorted(touched, key=lambda c: len(masks_by_component[c]))
    full_mask = (1 << len(viable)) - 1
    budget = [limits.max_certainty_nodes]
    failed: set[tuple[int, int]] = set()
    chosen_repairs: list[frozenset[RowKey]] = []

    def fill_remaining(position: int) -> None:
        for component_id in order[position:]:
            chosen_repairs.append(component_repairs[component_id][0])

    def dfs(position: int, alive: int) -> bool:
        budget[0] -= 1
        if budget[0] < 0:
            raise RepairLimitExceeded(
                "certainty search exceeded max_certainty_nodes=%d"
                % (limits.max_certainty_nodes,)
            )
        if alive == 0:
            fill_remaining(position)
            return True
        if position == len(order):
            return False
        state = (position, alive)
        if state in failed:
            return False
        component_id = order[position]
        for mask, repair in masks_by_component[component_id]:
            chosen_repairs.append(repair)
            if dfs(position + 1, alive & mask):
                return True
            chosen_repairs.pop()
        failed.add(state)
        return False

    if not dfs(0, full_mask):
        return None

    combined: set[RowKey] = set()
    for repair in chosen_repairs:
        combined.update(repair)
    for component_id, repairs in enumerate(component_repairs):
        if component_id not in masks_by_component:
            combined.update(repairs[0])
    return Witness(repair=frozenset(combined), derivation=None)


__all__ = [
    "AnalysisReport",
    "CandidateVerdict",
    "ComponentReport",
    "Derivation",
    "InfeasiblePins",
    "Limits",
    "Pin",
    "RepairError",
    "RepairLimitExceeded",
    "STATUS_ABSENT",
    "STATUS_CERTAIN",
    "STATUS_CONTINGENT",
    "Witness",
    "analyse",
    "delete_row",
    "keep_row",
]
