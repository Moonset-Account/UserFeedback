"""Feasibility checking for null states plus integer difference constraints."""

from __future__ import annotations

from dataclasses import dataclass, field
from math import inf
from typing import Any

from .linear import ZERO_VAR, Cube, Diff, Neq, comparison_cube
from .stats import RowGroupSummary


@dataclass
class Feasibility:
    feasible: bool
    reason: str | None = None
    resolved_nulls: dict[str, bool] = field(default_factory=dict)
    values: dict[str, int | None] | None = None
    ranges: dict[str, tuple[int | None, int | None]] = field(default_factory=dict)
    active_diffs: tuple[Diff, ...] = ()
    active_neqs: tuple[Neq, ...] = ()
    negative_cycle: bool = False


def _stats_forced_nulls(summary: RowGroupSummary) -> dict[str, bool]:
    forced: dict[str, bool] = {}
    for name, stats in summary.columns.items():
        if stats.all_null:
            forced[name] = True
        elif not stats.may_be_null:
            forced[name] = False
    return forced


def _relation_parts(relation: Any) -> tuple[tuple[Diff, ...], tuple[Neq, ...]]:
    cube = comparison_cube(relation.op, _column(relation.left), _column(relation.right), str(relation))
    return cube.diffs, cube.neqs


def _column(name: str):
    from .expressions import Column

    return Column(name)


def _floyd(variables: list[str], diffs: tuple[Diff, ...] | list[Diff]):
    n = len(variables)
    index = {name: i for i, name in enumerate(variables)}
    dist: list[list[Any]] = [[inf] * n for _ in range(n)]
    for i in range(n):
        dist[i][i] = 0
    for diff in diffs:
        i = index[diff.u]
        j = index[diff.v]
        if diff.bound < dist[i][j]:
            dist[i][j] = diff.bound

    for k in range(n):
        dk = dist[k]
        for i in range(n):
            dik = dist[i][k]
            if dik == inf:
                continue
            di = dist[i]
            for j in range(n):
                dkj = dk[j]
                if dkj == inf:
                    continue
                candidate = dik + dkj
                if candidate < di[j]:
                    di[j] = candidate

    negative = any(dist[i][i] < 0 for i in range(n))
    return dist, index, negative


def _ranges_from_matrix(dist, index: dict[str, int]) -> dict[str, tuple[int | None, int | None]]:
    result: dict[str, tuple[int | None, int | None]] = {}
    if ZERO_VAR not in index:
        return result
    z = index[ZERO_VAR]
    for name, i in index.items():
        if name == ZERO_VAR:
            continue
        upper_value = dist[i][z]
        lower_edge = dist[z][i]
        upper = None if upper_value == inf else int(upper_value)
        lower = None if lower_edge == inf else int(-lower_edge)
        result[name] = (lower, upper)
    return result


def find_negative_cycle(
    variables: list[str], diffs: tuple[Diff, ...] | list[Diff]
) -> tuple[Diff, ...] | None:
    """Return one negative cycle as ordered difference constraints, if present."""
    n = len(variables)
    index = {name: i for i, name in enumerate(variables)}
    distance = [0] * n
    parent = [-1] * n
    parent_diff: list[Diff | None] = [None] * n
    last = -1

    for _iteration in range(n):
        last = -1
        for diff in diffs:
            u = index[diff.u]
            v = index[diff.v]
            candidate = distance[v] + diff.bound
            if candidate < distance[u]:
                distance[u] = candidate
                parent[u] = v
                parent_diff[u] = diff
                last = u
        if last == -1:
            return None

    if last == -1:
        return None

    node = last
    for _ in range(n):
        node = parent[node]
        if node == -1:
            return None

    start = node
    cycle: list[Diff] = []
    current = start
    while True:
        diff = parent_diff[current]
        if diff is None:
            return None
        cycle.append(diff)
        current = parent[current]
        if current == start:
            break
        if current == -1 or len(cycle) > n:
            return None
    return tuple(cycle)


def format_diff(diff: Diff) -> str:
    if diff.u == ZERO_VAR and diff.v == ZERO_VAR:
        return f"0 <= {diff.bound}"
    if diff.v == ZERO_VAR:
        return f"{diff.u} <= {diff.bound}"
    if diff.u == ZERO_VAR:
        return f"{diff.v} >= {-diff.bound}"
    return f"{diff.u} - {diff.v} <= {diff.bound}"


def describe_constraint_contradiction(
    variables: list[str],
    diffs: tuple[Diff, ...] | list[Diff],
    ranges: dict[str, tuple[int | None, int | None]],
) -> str:
    cycle = find_negative_cycle(variables, diffs)
    if cycle:
        total = sum(item.bound for item in cycle)
        details = "；".join(format_diff(item) for item in cycle)
        return f"约束形成负环（{details}），边界和为 {total} < 0，无合法整数取值"

    for name, (lower, upper) in ranges.items():
        if lower is not None and upper is not None and lower > upper:
            return f"列 {name} 的联合整数区间为 [{lower}, {upper}]，下界 {lower} 大于上界 {upper}"

    return "整数约束联合后无解"


def _bellman_assignment(variables: list[str], diffs: tuple[Diff, ...] | list[Diff]) -> dict[str, int]:
    n = len(variables)
    index = {name: i for i, name in enumerate(variables)}
    distance = [0] * n

    updated = False
    for _iteration in range(n):
        updated = False
        for diff in diffs:
            u = index[diff.u]
            v = index[diff.v]
            candidate = distance[v] + diff.bound
            if candidate < distance[u]:
                distance[u] = candidate
                updated = True
        if not updated:
            break

    if updated:
        # Floyd already guards this; keep a defensive fallback.
        dist, _, negative = _floyd(variables, diffs)
        if negative:
            raise RuntimeError("cannot assign values to a negative-cycle constraint set")

    z = index[ZERO_VAR]
    shift = distance[z]
    return {name: distance[i] - shift for name, i in index.items()}


def _try_satisfy_neqs(
    variables: list[str],
    diffs: tuple[Diff, ...],
    neqs: tuple[Neq, ...],
) -> tuple[bool, tuple[Diff, ...], Any, dict[str, int]]:
    dist, index, negative = _floyd(variables, diffs)
    if negative:
        return False, diffs, dist, index

    if not neqs:
        return True, diffs, dist, index

    neq = neqs[0]
    remaining = neqs[1:]
    u = index[neq.u]
    v = index[neq.v]
    upper_value = dist[u][v]
    lower_value = -dist[v][u] if dist[v][u] != inf else None
    upper = None if upper_value == inf else int(upper_value)
    lower = None if lower_value is None else int(lower_value)
    value = neq.value

    # The forbidden point is outside the inferred difference interval.
    if lower is not None and value < lower:
        return _try_satisfy_neqs(variables, diffs, remaining)
    if upper is not None and value > upper:
        return _try_satisfy_neqs(variables, diffs, remaining)

    # The difference domain is exactly the forbidden singleton.
    if lower is not None and upper is not None and lower == upper == value:
        return False, diffs, dist, index

    less_diff = Diff(neq.u, neq.v, value - 1)  # u-v <= value-1
    ok, final_diffs, final_dist, final_index = _try_satisfy_neqs(
        variables, diffs + (less_diff,), remaining
    )
    if ok:
        return True, final_diffs, final_dist, final_index

    greater_diff = Diff(neq.v, neq.u, -value - 1)  # u-v >= value+1
    ok, final_diffs, final_dist, final_index = _try_satisfy_neqs(
        variables, diffs + (greater_diff,), remaining
    )
    if ok:
        return True, final_diffs, final_dist, final_index

    return False, diffs, dist, index


def check_cube(cube: Cube, summary: RowGroupSummary) -> Feasibility:
    if cube.conflict is not None:
        return Feasibility(False, cube.conflict)

    forced = _stats_forced_nulls(summary)
    resolved: dict[str, bool] = dict(forced)
    for name, state in cube.nulls:
        if name in resolved and resolved[name] != state:
            if state:
                reason = f"统计信息确认 {name} 不可能为 NULL，但该分支要求 {name} IS NULL"
            else:
                reason = f"统计信息确认 {name} 全为 NULL，但该分支要求 {name} IS NOT NULL"
            return Feasibility(False, reason, resolved_nulls=dict(resolved))
        resolved[name] = state

    variables_set: set[str] = {ZERO_VAR}
    variables_set.update(summary.columns.keys())
    variables_set.update(resolved.keys())
    for diff in cube.diffs:
        variables_set.update((diff.u, diff.v))
    for neq in cube.neqs:
        variables_set.update((neq.u, neq.v))

    active_diffs: list[Diff] = list(cube.diffs)
    active_neqs: list[Neq] = list(cube.neqs)

    # Reject malformed cubes that constrain a row known to be NULL.
    for diff in active_diffs:
        for name in (diff.u, diff.v):
            if name != ZERO_VAR and resolved.get(name) is True:
                return Feasibility(
                    False,
                    f"{name} 为 NULL 时整数比较结果不是 TRUE/FALSE",
                    resolved_nulls=resolved,
                )
    for neq in active_neqs:
        for name in (neq.u, neq.v):
            if name != ZERO_VAR and resolved.get(name) is True:
                return Feasibility(
                    False,
                    f"{name} 为 NULL 时整数比较结果不是 TRUE/FALSE",
                    resolved_nulls=resolved,
                )

    # Inclusive min/max become difference bounds against fixed zero.
    for name, stats in summary.columns.items():
        if resolved.get(name) is True:
            continue
        if stats.lower is not None:
            active_diffs.append(Diff(ZERO_VAR, name, -stats.lower))
        if stats.upper is not None:
            active_diffs.append(Diff(name, ZERO_VAR, stats.upper))

    # Confirmed row-wise relations apply only when both sides are non-NULL.
    for relation in summary.relationships:
        left_state = resolved.get(relation.left)
        right_state = resolved.get(relation.right)
        if left_state is True or right_state is True:
            continue
        if left_state is False and right_state is False:
            diffs, neqs = _relation_parts(relation)
            active_diffs.extend(diffs)
            active_neqs.extend(neqs)
            variables_set.update((relation.left, relation.right))

    variables = sorted(variables_set)
    feasible, final_diffs, _dist, _index = _try_satisfy_neqs(
        variables, tuple(active_diffs), tuple(active_neqs)
    )

    ranges = _ranges_from_matrix(_dist, _index)
    if not feasible:
        dist, index, negative_cycle = _floyd(variables, active_diffs)
        inferred_ranges = _ranges_from_matrix(dist, index)
        if negative_cycle:
            reason = describe_constraint_contradiction(variables, active_diffs, inferred_ranges)
        else:
            reason = describe_constraint_contradiction(variables, active_diffs, inferred_ranges)
        return Feasibility(
            False,
            reason,
            resolved_nulls=resolved,
            ranges=inferred_ranges,
            active_diffs=tuple(active_diffs),
            active_neqs=tuple(active_neqs),
            negative_cycle=negative_cycle,
        )

    raw_values = _bellman_assignment(variables, final_diffs)
    constrained_names = {
        name
        for diff in active_diffs
        for name in (diff.u, diff.v)
        if name != ZERO_VAR
    }
    values: dict[str, int | None] = {}
    for name in variables:
        if name == ZERO_VAR:
            continue
        state = resolved.get(name)
        if state is True:
            values[name] = None
        elif state is False or name in constrained_names:
            values[name] = int(raw_values[name])
        else:
            # Unmentioned nullable column: choose NULL so optional row-wise
            # relations do not constrain this witness row.
            stats = summary.columns.get(name)
            if stats is not None and not stats.may_be_null:
                values[name] = int(raw_values[name])
            else:
                values[name] = None

    # Ensure every declared stats column appears in the witness.
    for name, stats in summary.columns.items():
        if name not in values:
            if stats.all_null:
                values[name] = None
            elif not stats.may_be_null:
                values[name] = int(raw_values.get(name, 0))
            else:
                values[name] = None

    return Feasibility(
        True,
        None,
        resolved_nulls=resolved,
        values=values,
        ranges=ranges,
        active_diffs=tuple(active_diffs),
        active_neqs=tuple(active_neqs),
    )
