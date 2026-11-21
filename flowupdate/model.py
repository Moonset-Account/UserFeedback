"""Forwarding-table model for the next-hop update problem.

A *forwarding table* maps every node to the next hop it uses on the way to the
single egress node (``"OUT"``).  Nodes that are not present in the mapping are
treated as having no route (they are reported as ``None``).

Model of delivery
-----------------
The single egress is special: it is *not* a router that forwards anywhere, it is
the destination.  For every other node ``n`` with next hop ``h``:

* ``h == "OUT"``  -> ``n`` is delivered to the egress directly.
* ``h`` is a node -> ``n`` relies on ``h`` being able to reach the egress.

A node is *delivered* when following next hops reaches ``"OUT"``.  Any node that
follows a cycle never reaches ``"OUT"``; such a node is a *forwarding loop*.

Every table used in this problem is required to be *functional*: each node has
exactly one next-hop table entry, so updates are expressed purely as value
changes and the mapping fully determines the forwarding graph.

This module also provides validation that makes the traffic guarantee explicit
and checkable: a table is *safe* when every node reaches ``"OUT"`` and the egress
has no outgoing next hop of its own.
"""

from __future__ import annotations

import json
from typing import Dict, List, Mapping, Optional, Tuple

EGRESS = "OUT"

ForwardingTable = Dict[str, Optional[str]]


class ModelError(ValueError):
    """Raised when input data does not describe a well-formed problem."""


# ---------------------------------------------------------------------------
# Building / normalising tables
# ---------------------------------------------------------------------------

def make_table(mapping: Mapping[str, Optional[str]]) -> ForwardingTable:
    """Return a plain ``dict`` copy of *mapping*.

    ``None`` is kept as-is (an unrouted node); callers that need full
    reachability should run :func:`validate_table` first.
    """

    if not isinstance(mapping, Mapping):
        raise ModelError("a forwarding table must be a mapping")
    table: ForwardingTable = {}
    for node, nxt in mapping.items():
        if not isinstance(node, str) or not node:
            raise ModelError(f"node names must be non-empty strings, got {node!r}")
        if node == EGRESS:
            raise ModelError(f"the egress name {EGRESS!r} cannot be a forwarding node")
        if nxt is not None and not isinstance(nxt, str):
            raise ModelError(f"next hop of {node!r} must be a string or None")
        if nxt is not None and not nxt:
            raise ModelError(f"next hop of {node!r} cannot be the empty string")
        table[node] = nxt
    return table


def validate_table(
    table: Mapping[str, Optional[str]],
    *,
    require_full: bool = True,
) -> None:
    """Raise :class:`ModelError` unless *table* satisfies the routing invariants.

    ``require_full`` demands that every node has a route (next hop not ``None``).
    Every node mentioned as a next hop must itself be a node of the table, and
    following next hops must reach the egress.  The egress may not appear as a
    key of the table.
    """

    if not isinstance(table, Mapping):
        raise ModelError("a forwarding table must be a mapping")
    if EGRESS in table:
        raise ModelError(
            f"the egress name {EGRESS!r} cannot appear as a forwarding node"
        )

    for node, nxt in table.items():
        if not isinstance(node, str) or not node:
            raise ModelError(f"invalid node name {node!r}")
        if nxt is None:
            if require_full:
                raise ModelError(f"node {node!r} has no next hop (unrouted)")
            continue
        if not isinstance(nxt, str) or not nxt:
            raise ModelError(f"invalid next hop {nxt!r} for node {node!r}")
        if nxt != EGRESS and nxt not in table:
            raise ModelError(
                f"node {node!r} points at unknown next hop {nxt!r}"
            )

    problems = find_loops(table)
    if problems:
        raise ModelError(
            "forwarding table contains a loop: "
            + "; ".join(sorted(problems))
        )
    unreachable = unreachable_nodes(table)
    if unreachable:
        raise ModelError(
            "nodes cannot reach the egress: " + ", ".join(sorted(unreachable))
        )


# ---------------------------------------------------------------------------
# Reachability analysis
# ---------------------------------------------------------------------------

def unreachable_nodes(table: Mapping[str, Optional[str]]) -> List[str]:
    """Return nodes of *table* from which the egress cannot be reached.

    A node with no next hop (``None``) is unreachable.  A node pointing at an
    unknown next hop is unreachable as well.
    """

    reachable: set[str] = set()

    def walk(start: str) -> bool:
        path: List[str] = []
        seen: set[str] = set()
        node = start
        while True:
            if node == EGRESS:
                for n in path:
                    reachable.add(n)
                return True
            if node in reachable:
                for n in path:
                    reachable.add(n)
                return True
            if node in seen:
                return False  # loop
            seen.add(node)
            path.append(node)
            nxt = table.get(node)
            if nxt is None:
                return False
            node = nxt

    bad: List[str] = []
    for node in table:
        if not walk(node):
            bad.append(node)
    return bad


def find_loops(table: Mapping[str, Optional[str]]) -> List[str]:
    """Return one readable description per forwarding loop, if any.

    Each description names the nodes of a strongly connected cycle and the
    index used for deterministic output.
    """

    loops: List[str] = []
    checked: set[str] = set()
    for start in table:
        if start in checked:
            continue
        path: List[str] = []
        index: Dict[str, int] = {}
        node = start
        while True:
            if node == EGRESS or node is None or node not in table:
                break
            if node in index:
                cycle = path[index[node]:]
                loops.append(" -> ".join(cycle + [cycle[0]]))
                checked.update(cycle)
                break
            if node in checked:
                break
            index[node] = len(path)
            path.append(node)
            node = table.get(node)
        checked.update(path)
    return loops


# ---------------------------------------------------------------------------
# Change analysis
# ---------------------------------------------------------------------------

def changed_nodes(
    old: Mapping[str, Optional[str]],
    new: Mapping[str, Optional[str]],
) -> List[str]:
    """Return the nodes whose next hop differs between *old* and *new*.

    A node present as a key in only one of the tables counts as changed: an
    absent entry is ``None`` and differs from any real next hop.
    """

    keys = set(old) | set(new)
    return sorted(k for k in keys if old.get(k) != new.get(k))


# ---------------------------------------------------------------------------
# JSON loading
# ---------------------------------------------------------------------------

def load_problem(path: str) -> Tuple[ForwardingTable, ForwardingTable, Dict[str, str]]:
    """Load a problem description from *path* (JSON).

    Accepted shapes::

        {"old": {...}, "new": {...}, "label": {...}}          # flat
        {"old": {"table": {...}}, "new": {"table": {...}}}    # wrapped

    The optional ``label`` maps node names to friendly descriptions and is
    returned unchanged (an empty dict when absent).
    """

    with open(path, "r", encoding="utf-8") as handle:
        try:
            data = json.load(handle)
        except json.JSONDecodeError as exc:
            raise ModelError(f"invalid JSON in {path!r}: {exc}") from exc
    if not isinstance(data, dict):
        raise ModelError("problem file must contain a JSON object")
    if "old" not in data or "new" not in data:
        raise ModelError("problem file must contain 'old' and 'new'")

    def table_of(value: object, name: str) -> ForwardingTable:
        if isinstance(value, dict) and "table" in value:
            value = value["table"]
        if not isinstance(value, dict):
            raise ModelError(f"'{name}' must be an object mapping nodes to next hops")
        return make_table(value)

    old = table_of(data["old"], "old")
    new = table_of(data["new"], "new")
    label = data.get("label", {})
    if not isinstance(label, dict):
        raise ModelError("'label' must be an object mapping node names to text")
    return old, new, {str(k): str(v) for k, v in label.items()}
