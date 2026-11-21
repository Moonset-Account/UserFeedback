"""Incremental reachability for recursive queries over directed graphs.

The operator maintains a set of root seeds and a directed edge set.  A batch of
seed/edge insertions and deletions is applied atomically; the returned
:class:`Delta` describes the *net* change of the set of nodes reachable from
any seed.  Only the affected region of the graph is walked -- components that
are completely disconnected from every changed seed/edge are never traversed.

Cycles are handled by recomputing reachability from live sources rather than by
counting incoming edges.  Consequently, removing the only external entry into a
cycle retracts the whole cycle, even though every node of the cycle still has an
internal predecessor.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any, Hashable, Iterable

Node = Hashable
Edge = tuple[Any, Any]

__all__ = ["Delta", "IncrementalReachability"]


@dataclass(frozen=True)
class Delta:
    """Net effect of one atomic batch.

    Attributes
    ----------
    added:
        Nodes that became reachable from the seed set in this batch.  These
        nodes were *not* reachable before the batch.
    removed:
        Nodes that stopped being reachable from the seed set in this batch.
        These nodes *were* reachable before the batch.
    reexpanded:
        Nodes in the affected region whose outgoing edges were re-expanded by
        this update.  Every node in ``reexpanded`` is reachable after the
        batch.  Unaffected components never appear here.

    ``added`` and ``removed`` are disjoint.  A batch that only rewires paths
    without changing the reachable set yields an empty delta (``is_empty`` is
    true) even if some nodes were re-expanded.
    """

    added: frozenset[Node]
    removed: frozenset[Node]
    reexpanded: frozenset[Node]

    @property
    def is_empty(self) -> bool:
        """True when the reachable set is unchanged by the batch."""
        return not self.added and not self.removed

    def __bool__(self) -> bool:
        return not self.is_empty

    def replay_onto(self, reachable: Iterable[Node]) -> set[Node]:
        """Apply this delta to a caller-held reachable set and return it.

        A caller that starts from the operator's initial reachable set and
        replays every delta (``state -= delta.removed; state |= delta.added``)
        always reconstructs the current reachable set.
        """
        state = set(reachable)
        state.difference_update(self.removed)
        state.update(self.added)
        return state


class IncrementalReachability:
    """Maintains ``reachable(seeds)`` under batched seed/edge mutations.

    Parameters
    ----------
    seeds:
        Initial root nodes.  Seeds are always reachable, even without edges.
    edges:
        Initial directed edges as ``(source, target)`` pairs.  Duplicate edges
        are ignored (the graph is a simple directed graph).  Self-loops are
        allowed.

    Notes
    -----
    * Nodes may be any hashable objects.
    * The graph is held in memory.  There is no persistence and the class is
      not thread-safe.
    * A node disappears from :attr:`nodes` once it is neither a seed nor an
      endpoint of a live edge; this does not affect the reachable set.
    """

    def __init__(
        self,
        seeds: Iterable[Node] = (),
        edges: Iterable[Edge] = (),
    ) -> None:
        self._out: dict[Node, set[Node]] = {}
        self._in: dict[Node, set[Node]] = {}
        self._seeds: set[Node] = set()
        self._reach: set[Node] = set()

        for edge in edges:
            u, v = self._coerce_edge(edge)
            self._insert_edge(u, v)
        self._seeds.update(seeds)

        # Initial reachability: plain BFS from every seed.
        seen = set(self._seeds)
        stack = list(seen)
        while stack:
            u = stack.pop()
            for v in self._out_adj(u):
                if v not in seen:
                    seen.add(v)
                    stack.append(v)
        self._reach = seen

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def apply(
        self,
        seeds_add: Iterable[Node] = (),
        seeds_remove: Iterable[Node] = (),
        edges_add: Iterable[Edge] = (),
        edges_remove: Iterable[Edge] = (),
    ) -> Delta:
        """Apply one atomic batch and return its net reachability delta.

        Batch semantics are *remove-then-add*: removals are applied first and
        additions second, so a seed or edge listed in both collections is
        present afterwards.  Duplicate entries are ignored, and the delta is
        computed from the state before the batch against the state after the
        whole batch -- no intermediate state is ever observed.  Consequently a
        replacement path supplied in the same batch as a deletion prevents the
        nodes it supports from being reported as removed (no flicker).
        """
        seeds_add = set(seeds_add)
        seeds_remove = set(seeds_remove)
        add_edges = {self._coerce_edge(e) for e in edges_add}
        remove_edges = {self._coerce_edge(e) for e in edges_remove}

        # Net structural changes only, with remove-then-add semantics:
        #   * an edge removed and re-added stays present either way, so it is
        #     only a net removal when the re-add is absent;
        #   * an edge added is a net addition whenever it does not currently
        #     exist -- even if it was also listed for removal (removing an
        #     absent edge is a no-op, the add still takes effect).
        net_removed_seeds = (self._seeds & seeds_remove) - seeds_add
        net_added_seeds = seeds_add - self._seeds
        net_removed_edges = [
            e for e in remove_edges - add_edges if self._edge_exists(e)
        ]
        net_added_edges = [e for e in add_edges if not self._edge_exists(e)]

        if (
            not net_removed_seeds
            and not net_added_seeds
            and not net_removed_edges
            and not net_added_edges
        ):
            return Delta(frozenset(), frozenset(), frozenset())

        old_reach = self._reach  # membership tests below refer to the pre-batch state

        # Candidate "entry points" of the affected region.
        #
        # * a removed seed and the target of a removed edge can lose support;
        # * an added seed and the target of an added edge can gain support.
        #
        # Everything whose status can possibly change is structurally reachable
        # (over the *new* edges) from one of these candidates.
        candidates: set[Node] = set()
        candidates.update(net_removed_seeds)
        for u, v in net_removed_edges:
            # Only an edge whose source was reachable (and whose target was
            # reachable) can have carried support; otherwise removing it cannot
            # change the reachable set.
            if u in old_reach and v in old_reach:
                candidates.add(v)
        candidates.update(net_added_seeds)
        for u, v in net_added_edges:
            # Only a new edge out of an already-reachable node can newly reach
            # ``v``.  If ``u`` becomes reachable within this same batch, ``u``
            # is itself part of the forward closure below, which then reaches
            # ``v`` as well.
            if u in old_reach and v not in old_reach:
                candidates.add(v)

        # Install the batch before computing the new reachability, so paths
        # added in the same batch already count as support.
        for u, v in net_removed_edges:
            self._remove_edge(u, v)
        for u, v in net_added_edges:
            self._insert_edge(u, v)
        self._seeds.difference_update(net_removed_seeds)
        self._seeds.update(net_added_seeds)

        if not candidates:
            # Only structurally irrelevant edges changed (their targets were
            # already unreachable); reachability cannot move.
            return Delta(frozenset(), frozenset(), frozenset())

        # Affected region: forward closure over the new edges.  Nodes outside
        # this region keep their old reachability status.
        affected: set[Node] = set(candidates)
        stack = list(candidates)
        while stack:
            u = stack.pop()
            for v in self._out_adj(u):
                if v not in affected:
                    affected.add(v)
                    stack.append(v)

        # Recompute reachability *inside* the affected region.  Sources are:
        #   * seeds inside the region;
        #   * boundary nodes: reachable nodes outside the region with an edge
        #     into it.
        # Nodes are never treated as sources merely because another in-region
        # node points at them; that is what makes cycles safe to retract.
        visited: set[Node] = set()
        queue: deque[Node] = deque()

        def mark(node: Node) -> None:
            if node not in visited:
                visited.add(node)
                queue.append(node)

        for v in affected:
            if v in self._seeds:
                mark(v)
        for v in affected:
            for u in self._in_adj(v):
                if u not in affected and u in old_reach:
                    mark(v)
                    break
        while queue:
            u = queue.popleft()
            for v in self._out_adj(u):
                if v in affected and v not in visited:
                    visited.add(v)
                    queue.append(v)

        removed: set[Node] = set()
        for v in affected:
            if v in old_reach and v not in visited:
                removed.add(v)
        added: set[Node] = {v for v in visited if v not in old_reach}

        # Update only the delta; never scan the entire reachable set.
        self._reach.difference_update(removed)
        self._reach.update(added)

        return Delta(
            added=frozenset(added),
            removed=frozenset(removed),
            reexpanded=frozenset(visited),
        )

    def add_seeds(self, seeds: Iterable[Node]) -> Delta:
        """Convenience wrapper for :meth:`apply` with only seed insertions."""
        return self.apply(seeds_add=seeds)

    def remove_seeds(self, seeds: Iterable[Node]) -> Delta:
        """Convenience wrapper for :meth:`apply` with only seed deletions."""
        return self.apply(seeds_remove=seeds)

    def add_edges(self, edges: Iterable[Edge]) -> Delta:
        """Convenience wrapper for :meth:`apply` with only edge insertions."""
        return self.apply(edges_add=edges)

    def remove_edges(self, edges: Iterable[Edge]) -> Delta:
        """Convenience wrapper for :meth:`apply` with only edge deletions."""
        return self.apply(edges_remove=edges)

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------
    @property
    def reachable(self) -> frozenset[Node]:
        """Current set of nodes reachable from any seed."""
        return frozenset(self._reach)

    def is_reachable(self, node: Node) -> bool:
        return node in self._reach

    @property
    def seeds(self) -> frozenset[Node]:
        return frozenset(self._seeds)

    @property
    def nodes(self) -> frozenset[Node]:
        """All nodes that are seeds or endpoints of live edges."""
        return frozenset(self._out) | frozenset(self._in) | frozenset(self._seeds)

    @property
    def edges(self) -> frozenset[Edge]:
        return frozenset(
            (u, v) for u, targets in self._out.items() for v in targets
        )

    def successors(self, node: Node) -> frozenset[Node]:
        return frozenset(self._out.get(node, ()))

    def predecessors(self, node: Node) -> frozenset[Node]:
        return frozenset(self._in.get(node, ()))

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return (
            f"{type(self).__name__}(seeds={len(self._seeds)}, "
            f"edges={sum(len(v) for v in self._out.values())}, "
            f"reachable={len(self._reach)})"
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _coerce_edge(edge: Edge) -> tuple[Any, Any]:
        try:
            u, v = edge
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"edge must be a (source, target) pair, got {edge!r}"
            ) from exc
        return u, v

    def _out_adj(self, node: Node) -> tuple[Node, ...]:
        """Outgoing neighbours to expand.

        Exposed as a method so tests (or instrumented subclasses) can observe
        exactly which nodes the operator re-expands.
        """
        return tuple(self._out.get(node, ()))

    def _in_adj(self, node: Node) -> tuple[Node, ...]:
        """Incoming neighbours examined when looking for boundary sources."""
        return tuple(self._in.get(node, ()))

    def _edge_exists(self, edge: tuple[Any, Any]) -> bool:
        u, v = edge
        return v in self._out.get(u, ())

    def _insert_edge(self, u: Node, v: Node) -> None:
        self._out.setdefault(u, set()).add(v)
        self._in.setdefault(v, set()).add(u)

    def _remove_edge(self, u: Node, v: Node) -> None:
        targets = self._out.get(u)
        if targets is not None:
            targets.discard(v)
            if not targets:
                del self._out[u]
        sources = self._in.get(v)
        if sources is not None:
            sources.discard(u)
            if not sources:
                del self._in[v]
