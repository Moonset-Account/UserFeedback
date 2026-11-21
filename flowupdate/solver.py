"""Minimum-round scheduling for loop-free next-hop updates.

Problem
-------
There is one egress (``"OUT"``).  Two forwarding tables, *old* and *new*, each
route every node to the egress.  At most nine nodes change their next hop.  The
controller pushes the changes in *rounds*: a round is a set of changed nodes
whose new next-hop values are installed together.  Within a round the commands
may take effect in **any order**, so any subset of the round may already be live
while the rest are not.  The next round is only sent once the whole round is
acknowledged.

A round is *safe* when, for **every** subset of its nodes (representing "these
have taken effect so far"), the mixed forwarding table routes every node to the
egress without a forwarding loop.  We look for a partition of the changed nodes
into the fewest safe rounds.

Key observation
---------------
A mixed table is fully determined by the set of changed nodes that carry their
*new* next hop; every other node keeps its old value.  With at most nine changed
nodes there are at most 2**9 = 512 such tables, so we evaluate every one of them
once and then run a breadth-first search over committed-node sets.  BFS therefore
returns a schedule with the provably minimum number of rounds.

This module contains no I/O and is safe to import.
"""

from __future__ import annotations

import itertools
from collections import deque
from dataclasses import dataclass
from typing import Dict, FrozenSet, Iterable, List, Mapping, Optional, Sequence, Tuple

from .model import (
    ForwardingTable,
    ModelError,
    changed_nodes,
    unreachable_nodes,
    validate_table,
)

DEFAULT_MAX_ROUND_SIZE = 9
DEFAULT_MAX_CHANGED = 9


class NoScheduleError(RuntimeError):
    """Raised when no ordering of the changes keeps every table loop-free."""


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Schedule:
    """A loop-free update plan: an ordered sequence of rounds.

    ``rounds`` is a tuple of rounds; each round is a sorted tuple of node names.
    ``changed`` is the sorted tuple of every node that differs between the old
    and new tables.  The rounds partition ``changed`` exactly.
    """

    rounds: Tuple[Tuple[str, ...], ...]
    changed: Tuple[str, ...]

    @property
    def round_count(self) -> int:
        return len(self.rounds)

    def as_dict(self) -> Dict[str, object]:
        return {
            "round_count": self.round_count,
            "changed": list(self.changed),
            "rounds": [list(r) for r in self.rounds],
        }


@dataclass(frozen=True)
class RoundCheck:
    """Result of checking a single round in isolation."""

    nodes: Tuple[str, ...]
    safe: bool
    failing_subset: Tuple[str, ...] = ()
    unreachable: Tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Table construction helpers
# ---------------------------------------------------------------------------

def table_for_state(
    old: Mapping[str, Optional[str]],
    new: Mapping[str, Optional[str]],
    applied: Iterable[str],
) -> ForwardingTable:
    """Return *old* with the next hops of *applied* nodes replaced by *new*."""

    table: ForwardingTable = dict(old)
    for node in applied:
        if node in new:
            table[node] = new[node]
    return table


def subsets(nodes: Sequence[str]) -> Iterable[Tuple[str, ...]]:
    """Yield every subset of *nodes* (including the empty one) as sorted tuples."""

    ordered = tuple(sorted(nodes))
    for mask in range(1 << len(ordered)):
        yield tuple(ordered[i] for i in range(len(ordered)) if mask & (1 << i))


# ---------------------------------------------------------------------------
# Round safety
# ---------------------------------------------------------------------------

def check_round(
    base: Mapping[str, Optional[str]],
    round_nodes: Sequence[str],
    new: Mapping[str, Optional[str]],
) -> RoundCheck:
    """Check that *round_nodes* can be installed on *base* in any order.

    Every subset of the round is applied to *base*; the resulting table must
    route all nodes to the egress.  ``failing_subset`` reports the first subset
    (in deterministic subset order) that fails, and ``unreachable`` the nodes
    that cannot reach the egress in that state.
    """

    nodes = tuple(sorted(round_nodes))
    for subset in subsets(nodes):
        table = table_for_state(base, new, subset)
        bad = unreachable_nodes(table)
        if bad:
            return RoundCheck(
                nodes=nodes,
                safe=False,
                failing_subset=subset,
                unreachable=tuple(sorted(bad)),
            )
    return RoundCheck(nodes=nodes, safe=True)


def round_is_safe(
    base: Mapping[str, Optional[str]],
    round_nodes: Sequence[str],
    new: Mapping[str, Optional[str]],
) -> bool:
    """Boolean convenience wrapper around :func:`check_round`."""

    return check_round(base, round_nodes, new).safe


# ---------------------------------------------------------------------------
# Safety table and search
# ---------------------------------------------------------------------------

def safe_states(
    old: Mapping[str, Optional[str]],
    new: Mapping[str, Optional[str]],
    changed: Sequence[str],
) -> Dict[FrozenSet[str], bool]:
    """Map every subset of *changed* to whether that mixed table is loop-free."""

    ordered = tuple(changed)
    result: Dict[FrozenSet[str], bool] = {}
    for subset in subsets(ordered):
        table = table_for_state(old, new, subset)
        result[frozenset(subset)] = not unreachable_nodes(table)
    return result


def solve(
    old: Mapping[str, Optional[str]],
    new: Mapping[str, Optional[str]],
    *,
    max_round_size: int = DEFAULT_MAX_ROUND_SIZE,
    max_changed: int = DEFAULT_MAX_CHANGED,
) -> Schedule:
    """Return a minimum-round loop-free schedule from *old* to *new*.

    Raises :class:`~flowupdate.model.ModelError` when the tables are malformed
    or the changed set is too large (nine by default), and
    :class:`NoScheduleError` when no safe ordering exists.
    """

    if max_round_size < 1:
        raise ModelError("max_round_size must be at least 1")
    if max_changed < 1:
        raise ModelError("max_changed must be at least 1")

    old_table: ForwardingTable = dict(old)
    new_table: ForwardingTable = dict(new)
    _require_same_nodes(old_table, new_table)
    validate_table(old_table)
    validate_table(new_table)

    changed = tuple(changed_nodes(old_table, new_table))
    if not changed:
        return Schedule(rounds=(), changed=())
    if len(changed) > max_changed:
        raise ModelError(
            f"{len(changed)} nodes change their next hop, but at most "
            f"{max_changed} are allowed"
        )

    states = safe_states(old_table, new_table, changed)
    if not states[frozenset()]:  # pragma: no cover - validate_table guards this
        raise ModelError("the old table is not loop-free")

    rounds = _bfs(states, frozenset(changed), max_round_size)
    if rounds is None:
        raise NoScheduleError(
            "no ordering of the next-hop changes keeps every intermediate "
            "forwarding table loop-free"
        )
    return Schedule(rounds=rounds, changed=changed)


def _require_same_nodes(
    old: Mapping[str, Optional[str]],
    new: Mapping[str, Optional[str]],
) -> None:
    missing_new = sorted(set(old) - set(new))
    missing_old = sorted(set(new) - set(old))
    if missing_new or missing_old:
        raise ModelError(
            "old and new tables must cover the same nodes "
            f"(only in old: {missing_new}; only in new: {missing_old})"
        )


def _bfs(
    states: Mapping[FrozenSet[str], bool],
    full: FrozenSet[str],
    max_round_size: int,
) -> Optional[Tuple[Tuple[str, ...], ...]]:
    changed = tuple(sorted(full))
    start: FrozenSet[str] = frozenset()
    parent: Dict[FrozenSet[str], Optional[FrozenSet[str]]] = {start: None}
    chosen: Dict[FrozenSet[str], Tuple[str, ...]] = {}

    def safe(state: FrozenSet[str], nodes: Tuple[str, ...]) -> bool:
        for subset in subsets(nodes):
            if not states[state | frozenset(subset)]:
                return False
        return True

    queue: deque[FrozenSet[str]] = deque([start])
    while queue:
        state = queue.popleft()
        if state == full:
            break
        remaining = [n for n in changed if n not in state]
        limit = min(max_round_size, len(remaining))
        # Deterministic round ordering: smallest rounds first, then lexicographic.
        for size in range(1, limit + 1):
            for nodes in itertools.combinations(remaining, size):
                if not safe(state, nodes):
                    continue
                nxt_state = state | frozenset(nodes)
                if nxt_state in parent:
                    continue
                parent[nxt_state] = state
                chosen[nxt_state] = nodes
                queue.append(nxt_state)

    if full not in parent:
        return None

    rounds: List[Tuple[str, ...]] = []
    cursor: Optional[FrozenSet[str]] = full
    while cursor is not None and parent[cursor] is not None:
        rounds.append(chosen[cursor])
        cursor = parent[cursor]
    rounds.reverse()
    return tuple(rounds)
