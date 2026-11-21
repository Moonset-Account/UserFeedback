"""Independent brute-force reference for cross-checking the DP planner.

Instead of reusing the planner's dynamic program, this module enumerates
*every* left-deep extension (order, broadcast/repartition choice, and
crossing key pair) with a plain recursion and no pruning or memoisation.
Tests compare ``plan_query`` against :func:`best_cost` on small instances;
a DP bug that prunes a state it should keep (or mis-propagates a
partition property) shows up as a cost mismatch.

The cost rules are re-derived here from the specification text rather than
imported from :mod:`joinplan.planner`.
"""

from __future__ import annotations

from dataclasses import dataclass

from joinplan.model import Plan, Query


@dataclass(frozen=True)
class RefStep:
    method: str
    right_table: str
    left_key: str
    right_key: str
    cost: int


class _Classes:
    """Tiny union-find over qualified column names."""

    def __init__(self, parent: dict[str, str] | None = None) -> None:
        self.parent: dict[str, str] = dict(parent or {})

    def add(self, column: str) -> None:
        self.parent.setdefault(column, column)

    def find(self, column: str) -> str:
        self.add(column)
        root = column
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[column] != root:
            self.parent[column], column = root, self.parent[column]
        return root

    def union(self, left: str, right: str) -> None:
        left_root, right_root = self.find(left), self.find(right)
        if left_root != right_root:
            self.parent[right_root] = left_root

    def members(self, column: str) -> frozenset[str]:
        root = self.find(column)
        return frozenset(name for name in list(self.parent) if self.find(name) == root)

    def clone(self) -> "_Classes":
        return _Classes(self.parent)


def _columns_of(query: Query, table_name: str) -> list[str]:
    columns: set[str] = set()
    for edge in query.edges:
        for reference in (edge.left, edge.right):
            owner, column = reference.split(".")
            if owner == table_name:
                columns.add(f"{owner}.{column}")
    table = query.table(table_name)
    if table.partition_key is not None:
        columns.add(f"{table_name}.{table.partition_key}")
    return sorted(columns)


def _crossing_edges(
    query: Query, joined: frozenset[str], right_table: str
) -> list[tuple[str, str]]:
    crossing = []
    for edge in query.edges:
        first_table = edge.left.split(".")[0]
        second_table = edge.right.split(".")[0]
        if first_table in joined and second_table == right_table:
            crossing.append((edge.left, edge.right))
        elif second_table in joined and first_table == right_table:
            crossing.append((edge.right, edge.left))
    return crossing


def _dfs(
    query: Query,
    joined: frozenset[str],
    classes: _Classes,
    partitions: frozenset[str] | None,
    total: int,
    best: list[int],
) -> None:
    """Explore every left-deep extension, recording the cheapest total."""
    if joined == query.table_names:
        if total < best[0]:
            best[0] = total
        return

    left_bytes = query.result_bytes(joined)
    for table in query.tables:
        if table.name in joined:
            continue
        crossing = _crossing_edges(query, joined, table.name)
        if not crossing:  # only tables connected to the left side may extend
            continue

        base_classes = classes.clone()
        for column in _columns_of(query, table.name):
            base_classes.add(column)
        for left_key, right_key in crossing:
            base_classes.union(left_key, right_key)

        for left_key, right_key in crossing:
            if query.broadcast_limit is None or table.nbytes <= query.broadcast_limit:
                next_partitions = (
                    None
                    if partitions is None
                    else base_classes.members(next(iter(partitions)))
                )
                _dfs(
                    query,
                    joined | {table.name},
                    base_classes,
                    next_partitions,
                    total + table.nbytes * (query.nodes - 1),
                    best,
                )

            left_free = partitions is not None and left_key in partitions
            right_free = (
                table.partition_key is not None
                and right_key == f"{table.name}.{table.partition_key}"
            )
            step_cost = (0 if left_free else left_bytes) + (
                0 if right_free else table.nbytes
            )
            _dfs(
                query,
                joined | {table.name},
                base_classes,
                base_classes.members(left_key),
                total + step_cost,
                best,
            )


def best_cost(query: Query) -> int:
    best = [10**18]
    for table in query.tables:
        classes = _Classes()
        for column in _columns_of(query, table.name):
            classes.add(column)
        partition = (
            None
            if table.partition_key is None
            else frozenset({f"{table.name}.{table.partition_key}"})
        )
        _dfs(query, frozenset({table.name}), classes, partition, 0, best)
    if best[0] == 10**18:
        raise AssertionError("reference enumerator found no plan")
    return best[0]


def assert_plan_shape(testcase, plan: Plan, query: Query) -> None:
    """Check internal consistency of a produced plan."""
    total = 0
    joined: frozenset[str] | None = None
    for index, step in enumerate(plan.steps):
        testcase.assertEqual(step.index, index)
        if joined is not None:
            testcase.assertEqual(step.left_tables, joined)
        testcase.assertEqual(step.cost, step.left_transfer + step.right_transfer)
        total += step.cost
        testcase.assertEqual(step.cumulative_cost, total)
        joined = step.result_tables
    testcase.assertEqual(total, plan.total_cost)
    testcase.assertEqual(plan.result_tables, query.table_names)
