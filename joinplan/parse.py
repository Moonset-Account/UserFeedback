"""Strict, defensive parsing/validation of a join-planning request.

The canonical input is JSON:

.. code-block:: json

    {
      "nodes": 4,
      "broadcast_limit": 1000000,
      "tables": [
        {"name": "A", "bytes": 1000, "partition_key": "k"},
        {"name": "B", "bytes": 2000}
      ],
      "edges": [{"left": "A.k", "right": "B.k"}],
      "outputs": [{"tables": ["A", "B"], "bytes": 500}]
    }

Validation rejects anything the planner cannot reason about:
unknown fields, duplicate tables/edges/estimates, non-integer or
negative sizes, self-edges, edges to unknown tables, graphs with more
than :data:`MAX_TABLES` tables or with disconnected components, and
output estimates that do not exactly cover the connectable table sets.
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Mapping

from .model import InputError, JoinEdge, Query, Table

MAX_TABLES = 7

_TOP_LEVEL_KEYS = frozenset({"nodes", "broadcast_limit", "tables", "edges", "outputs"})
_TABLE_KEYS = frozenset({"name", "bytes", "partition_key"})
_EDGE_KEYS = frozenset({"left", "right"})
_OUTPUT_KEYS = frozenset({"tables", "bytes"})


def parse_query(data: Any) -> Query:
    """Validate a decoded JSON object and return a :class:`Query`."""
    if not isinstance(data, Mapping):
        raise InputError("input must be a JSON object at the top level")

    unknown = set(data) - _TOP_LEVEL_KEYS
    if unknown:
        raise InputError("unknown input field(s): " + ", ".join(sorted(map(str, unknown))))

    for required in ("nodes", "tables"):
        if required not in data:
            raise InputError(f"missing required field: {required}")

    nodes = _as_int(data["nodes"], "nodes", minimum=1)
    broadcast_limit = None
    if data.get("broadcast_limit") is not None:
        broadcast_limit = _as_int(data["broadcast_limit"], "broadcast_limit", minimum=0)

    raw_tables = data["tables"]
    if not isinstance(raw_tables, (list, tuple)):
        raise InputError("tables must be a list")
    if not raw_tables:
        raise InputError("at least one table is required")
    if len(raw_tables) > MAX_TABLES:
        raise InputError(
            f"at most {MAX_TABLES} tables are supported, got {len(raw_tables)}"
        )

    tables: list[Table] = []
    seen_names: set[str] = set()
    for position, raw in enumerate(raw_tables):
        where = f"tables[{position}]"
        table = _parse_table(raw, where)
        if table.name in seen_names:
            raise InputError(f"duplicate table name: {table.name}")
        seen_names.add(table.name)
        tables.append(table)

    table_by_name = {table.name: table for table in tables}

    raw_edges = data.get("edges", [])
    if not isinstance(raw_edges, (list, tuple)):
        raise InputError("edges must be a list")
    edges: list[JoinEdge] = []
    seen_edges: set[tuple[str, str]] = set()
    for position, raw in enumerate(raw_edges):
        where = f"edges[{position}]"
        edge = _parse_edge(raw, where, table_by_name)
        canonical = tuple(sorted((edge.left, edge.right)))
        if canonical in seen_edges:
            raise InputError(
                f"duplicate join edge between {canonical[0]} and {canonical[1]}"
            )
        seen_edges.add(canonical)
        edges.append(edge)

    if len(tables) > 1:
        components = _components(table_by_name, edges)
        if len({frozenset(component) for component in components}) > 1:
            raise InputError(
                "query graph is disconnected: no left-deep plan can join all tables "
                "using only connection edges"
            )

    outputs = _parse_outputs(data.get("outputs", []), table_by_name, edges)

    return Query(
        nodes=nodes,
        tables=tuple(tables),
        edges=tuple(edges),
        output_bytes=outputs,
        broadcast_limit=broadcast_limit,
    )


def loads_query(text: str) -> Query:
    """Parse JSON text into a validated :class:`Query`."""
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InputError(f"invalid JSON: {exc.msg} at line {exc.lineno} column {exc.colno}") from exc
    return parse_query(decoded)


def load_query(path: str) -> Query:
    """Read and validate a JSON request from ``path`` (UTF-8)."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        raise InputError(f"cannot read input file {path!r}: {exc.strerror or exc}") from exc
    return loads_query(text)


def _parse_table(raw: Any, where: str) -> Table:
    if not isinstance(raw, Mapping):
        raise InputError(f"{where} must be an object")
    unknown = set(raw) - _TABLE_KEYS
    if unknown:
        raise InputError(f"{where} has unknown field(s): " + ", ".join(sorted(map(str, unknown))))
    if "name" not in raw:
        raise InputError(f"{where} is missing required field: name")
    if "bytes" not in raw:
        raise InputError(f"{where} is missing required field: bytes")

    name = raw["name"]
    if not isinstance(name, str) or not name.strip():
        raise InputError(f"{where}.name must be a non-empty string")
    if any(character.isspace() for character in name):
        raise InputError(f"{where}.name must not contain whitespace: {name!r}")

    nbytes = _as_int(raw["bytes"], f"{where}.bytes", minimum=0)

    partition_key = raw.get("partition_key")
    if partition_key is not None:
        if not isinstance(partition_key, str) or not partition_key.strip():
            raise InputError(f"{where}.partition_key must be a non-empty string or null")
        if "." in partition_key:
            raise InputError(f"{where}.partition_key must be a bare column name, got {partition_key!r}")

    return Table(name=name, nbytes=nbytes, partition_key=partition_key)


def _parse_edge(raw: Any, where: str, table_by_name: Mapping[str, Table]) -> JoinEdge:
    if isinstance(raw, Mapping):
        unknown = set(raw) - _EDGE_KEYS
        if unknown:
            raise InputError(f"{where} has unknown field(s): " + ", ".join(sorted(map(str, unknown))))
        if "left" not in raw or "right" not in raw:
            raise InputError(f"{where} must contain both 'left' and 'right'")
        left, right = raw["left"], raw["right"]
    elif isinstance(raw, (list, tuple)):
        if len(raw) != 2:
            raise InputError(f"{where} must be a two-element list of column references")
        left, right = raw[0], raw[1]
    else:
        raise InputError(f"{where} must be an object or a two-element list")

    left_table, left_column = _split_column(left, f"{where}.left")
    right_table, right_column = _split_column(right, f"{where}.right")
    if left_table not in table_by_name:
        raise InputError(f"{where}.left references unknown table: {left_table!r}")
    if right_table not in table_by_name:
        raise InputError(f"{where}.right references unknown table: {right_table!r}")
    if left_table == right_table:
        raise InputError(
            f"{where} is a self-join on table {left_table!r}; only joins between "
            "distinct tables are supported"
        )
    return JoinEdge(left=f"{left_table}.{left_column}", right=f"{right_table}.{right_column}")


def _split_column(reference: Any, where: str) -> tuple[str, str]:
    if not isinstance(reference, str):
        raise InputError(f"{where} must be a string of the form 'Table.column'")
    parts = reference.split(".")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise InputError(f"{where} must be of the form 'Table.column', got {reference!r}")
    return parts[0], parts[1]


def _parse_outputs(
    raw_outputs: Any,
    table_by_name: Mapping[str, Table],
    edges: Iterable[JoinEdge],
) -> Mapping[frozenset[str], int]:
    if not isinstance(raw_outputs, (list, tuple)):
        raise InputError("outputs must be a list")

    outputs: dict[frozenset[str], int] = {}
    for position, raw in enumerate(raw_outputs):
        where = f"outputs[{position}]"
        if not isinstance(raw, Mapping):
            raise InputError(f"{where} must be an object")
        unknown = set(raw) - _OUTPUT_KEYS
        if unknown:
            raise InputError(f"{where} has unknown field(s): " + ", ".join(sorted(map(str, unknown))))
        if "tables" not in raw or "bytes" not in raw:
            raise InputError(f"{where} must contain both 'tables' and 'bytes'")

        names = raw["tables"]
        if not isinstance(names, (list, tuple)):
            raise InputError(f"{where}.tables must be a list of table names")
        if len(names) < 2:
            raise InputError(
                f"{where}.tables must list at least two tables; single-table sizes "
                "come from the table definitions"
            )
        if len(set(map(repr, names))) != len(names):
            raise InputError(f"{where}.tables contains duplicate names")
        for name in names:
            if not isinstance(name, str) or name not in table_by_name:
                raise InputError(f"{where}.tables references unknown table: {name!r}")

        key = frozenset(names)
        if key in outputs:
            raise InputError(
                f"duplicate output estimate for table set: {', '.join(sorted(key))}"
            )
        outputs[key] = _as_int(raw["bytes"], f"{where}.bytes", minimum=0)

    required = connectable_sets(table_by_name, edges)
    missing = required - set(outputs)
    if missing:
        raise InputError(
            "missing output byte estimate(s) for connectable table set(s): "
            + "; ".join(", ".join(sorted(group)) for group in sorted(missing, key=lambda s: (len(s), sorted(s))))
        )
    extra = set(outputs) - required
    if extra:
        raise InputError(
            "output byte estimate supplied for non-joinable table set(s): "
            + "; ".join(", ".join(sorted(group)) for group in sorted(extra, key=lambda s: (len(s), sorted(s))))
        )
    return outputs


def connectable_sets(
    table_by_name: Mapping[str, Table], edges: Iterable[JoinEdge]
) -> set[frozenset[str]]:
    """All connected table sets of size >= 2 (the sets a join can produce)."""
    names = sorted(table_by_name)
    adjacency = {name: set() for name in names}
    for edge in edges:
        left_table = edge.left.split(".")[0]
        right_table = edge.right.split(".")[0]
        adjacency[left_table].add(right_table)
        adjacency[right_table].add(left_table)

    result: set[frozenset[str]] = set()
    components = _components(table_by_name, edges)
    for component in components:
        component = sorted(component)
        if len(component) < 2:
            continue
        for mask in range(1, 1 << len(component)):
            chosen = [component[index] for index in range(len(component)) if mask & (1 << index)]
            if len(chosen) < 2:
                continue
            if _is_connected(chosen, adjacency):
                result.add(frozenset(chosen))
    return result


def _components(
    table_by_name: Mapping[str, Table], edges: Iterable[JoinEdge]
) -> list[set[str]]:
    names = sorted(table_by_name)
    adjacency = {name: set() for name in names}
    for edge in edges:
        left_table = edge.left.split(".")[0]
        right_table = edge.right.split(".")[0]
        adjacency[left_table].add(right_table)
        adjacency[right_table].add(left_table)

    seen: set[str] = set()
    components: list[set[str]] = []
    for name in names:
        if name in seen:
            continue
        stack = [name]
        component: set[str] = set()
        seen.add(name)
        while stack:
            current = stack.pop()
            component.add(current)
            for neighbour in adjacency[current]:
                if neighbour not in seen:
                    seen.add(neighbour)
                    stack.append(neighbour)
        components.append(component)
    return components


def _is_connected(chosen: Iterable[str], adjacency: Mapping[str, set[str]]) -> bool:
    chosen = list(chosen)
    allowed = set(chosen)
    start = chosen[0]
    seen = {start}
    stack = [start]
    while stack:
        current = stack.pop()
        for neighbour in adjacency[current]:
            if neighbour in allowed and neighbour not in seen:
                seen.add(neighbour)
                stack.append(neighbour)
    return len(seen) == len(allowed)


def _as_int(value: Any, field: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InputError(f"{field} must be an integer, got {type(value).__name__}")
    if value < minimum:
        raise InputError(f"{field} must be >= {minimum}, got {value}")
    return value
