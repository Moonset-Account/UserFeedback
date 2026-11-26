"""Directed relay graph and deterministic simple-route enumeration."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from .model import DEVICE, PRESENCE, AdjudicationInputError


@dataclass(frozen=True)
class RelayEdge:
    source: str
    target: str
    edge_id: str = ""
    allowed_kinds: frozenset[str] | None = None
    authorization_costs: Mapping[str, int] = field(default_factory=dict)
    capacity: int | None = None

    def __post_init__(self) -> None:
        if not self.source or not self.target:
            raise AdjudicationInputError("relay edge source and target are required")
        if self.allowed_kinds is not None:
            object.__setattr__(self, "allowed_kinds", frozenset(self.allowed_kinds))
        costs = dict(self.authorization_costs or {})
        for key, amount in costs.items():
            if not isinstance(amount, int) or isinstance(amount, bool) or amount < 0:
                raise AdjudicationInputError("authorization cost must be a non-negative integer")
        object.__setattr__(self, "authorization_costs", costs)
        if self.capacity is not None and (
            not isinstance(self.capacity, int) or isinstance(self.capacity, bool) or self.capacity < 1
        ):
            raise AdjudicationInputError("edge capacity must be a positive integer or null")

    @classmethod
    def device_only(
        cls,
        source: str,
        target: str,
        edge_id: str = "",
        *,
        capacity: int | None = None,
    ) -> "RelayEdge":
        """An edge that relays only a device proximity proof."""
        return cls(source, target, edge_id, frozenset({DEVICE}), {}, capacity)

    @classmethod
    def presence_handoff(
        cls,
        source: str,
        target: str,
        edge_id: str = "",
        *,
        authorization: str = "presence_authorization",
        amount: int = 1,
        also_carries_device: bool = False,
        capacity: int | None = None,
    ) -> "RelayEdge":
        """An edge that consumes one presence authorization.

        By default only presence proofs may traverse it.  A device proof may be
        allowed through with ``also_carries_device`` if it itself carries the
        configured authorization unit.
        """
        kinds = {PRESENCE}
        if also_carries_device:
            kinds.add(DEVICE)
        return cls(source, target, edge_id, frozenset(kinds), {authorization: amount}, capacity)

    @property
    def mode(self) -> str:
        if self.allowed_kinds == frozenset({DEVICE}) and not self.authorization_costs:
            return "device_only"
        if self.authorization_costs.get("presence_authorization") == 1:
            return "presence_consuming"
        if self.allowed_kinds is None and not self.authorization_costs:
            return "open"
        return "custom"

    def allows(self, proof_kind: str) -> bool:
        return self.allowed_kinds is None or proof_kind in self.allowed_kinds


@dataclass(frozen=True)
class Route:
    edges: tuple[RelayEdge, ...]

    @property
    def edge_ids(self) -> tuple[str, ...]:
        return tuple(e.edge_id for e in self.edges)

    @property
    def nodes(self) -> tuple[str, ...]:
        if not self.edges:
            return ()
        return (self.edges[0].source, *[e.target for e in self.edges])

    def total_authorization_costs(self) -> dict[str, int]:
        total: dict[str, int] = {}
        for edge in self.edges:
            for key, amount in edge.authorization_costs.items():
                total[key] = total.get(key, 0) + amount
        return total

    @property
    def edge_capacity_ids(self) -> tuple[str, ...]:
        return tuple(e.edge_id for e in self.edges if e.capacity is not None)


class RelayGraph:
    """A small directed multigraph of relay/handoff edges."""

    def __init__(self, edges: Iterable[RelayEdge | Mapping[str, Any]] = ()) -> None:
        normalized: list[RelayEdge] = []
        for index, raw in enumerate(edges, start=1):
            edge = edge_from_dict(raw) if isinstance(raw, Mapping) else raw
            if not isinstance(edge, RelayEdge):
                raise AdjudicationInputError("graph edges must be RelayEdge or mappings")
            if not edge.edge_id:
                edge = replace(edge, edge_id=f"edge-{index}:{edge.source}->{edge.target}")
            normalized.append(edge)

        ids = [e.edge_id for e in normalized]
        if len(ids) != len(set(ids)):
            raise AdjudicationInputError(f"relay edge ids must be unique: {ids}")

        self.edges: tuple[RelayEdge, ...] = tuple(normalized)
        self.nodes: frozenset[str] = frozenset(
            node for edge in self.edges for node in (edge.source, edge.target)
        )
        self._by_id = {edge.edge_id: edge for edge in self.edges}
        self._adjacency: dict[str, list[RelayEdge]] = {}
        for edge in self.edges:
            self._adjacency.setdefault(edge.source, []).append(edge)

    def get(self, edge_id: str) -> RelayEdge:
        return self._by_id[edge_id]

    def routes(
        self,
        source: str,
        target: str,
        proof_kind: str | None = None,
        *,
        max_routes: int | None = None,
    ) -> list[Route]:
        """Return deterministic simple routes matching ``proof_kind``.

        An empty route means proof and operation are already at the same graph
        node.  Route order is stable by edge id.
        """
        if source == target:
            return [Route(())]

        results: list[Route] = []

        def visit(node: str, edges: tuple[RelayEdge, ...], visited: frozenset[str]) -> None:
            if max_routes is not None and len(results) >= max_routes:
                return
            outgoing = sorted(self._adjacency.get(node, ()), key=lambda e: (e.edge_id, e.target))
            for edge in outgoing:
                if proof_kind is not None and not edge.allows(proof_kind):
                    continue
                if edge.target in visited:
                    continue
                next_edges = edges + (edge,)
                if edge.target == target:
                    results.append(Route(next_edges))
                    if max_routes is not None and len(results) >= max_routes:
                        return
                else:
                    visit(edge.target, next_edges, visited | {edge.target})
                    if max_routes is not None and len(results) >= max_routes:
                        return

        visit(source, (), frozenset({source}))
        return results

    def to_dict(self) -> dict[str, Any]:
        return {
            "edges": [
                {
                    "edge_id": e.edge_id,
                    "source": e.source,
                    "target": e.target,
                    "mode": e.mode,
                    "allowed_kinds": sorted(e.allowed_kinds) if e.allowed_kinds is not None else None,
                    "authorization_costs": dict(e.authorization_costs),
                    "capacity": e.capacity,
                }
                for e in self.edges
            ]
        }


def edge_from_dict(value: RelayEdge | Mapping[str, Any]) -> RelayEdge:
    if isinstance(value, RelayEdge):
        return value
    if not isinstance(value, Mapping):
        raise AdjudicationInputError("edge must be a RelayEdge or mapping")
    source = value.get("source", value.get("from"))
    target = value.get("target", value.get("to"))
    if source is None or target is None:
        raise AdjudicationInputError("edge requires source/from and target/to")
    edge_id = value.get("edge_id", value.get("id", ""))
    mode = value.get("mode", value.get("type", "custom"))
    capacity = value.get("capacity")

    if mode in ("device_only", "device"):
        return RelayEdge.device_only(str(source), str(target), str(edge_id or ""), capacity=capacity)
    if mode in ("presence", "presence_handoff", "presence_consuming"):
        amount = int(value.get("amount", value.get("cost", 1)))
        authorization = value.get("authorization", "presence_authorization")
        return RelayEdge.presence_handoff(
            str(source),
            str(target),
            str(edge_id or ""),
            authorization=str(authorization),
            amount=amount,
            also_carries_device=bool(value.get("also_carries_device", False)),
            capacity=capacity,
        )
    if mode in ("open", "any"):
        return RelayEdge(
            str(source), str(target), str(edge_id or ""), None,
            value.get("authorization_costs", {}), capacity,
        )

    allowed = value.get("allowed_kinds", value.get("kinds"))
    if allowed is not None:
        allowed = frozenset(allowed if not isinstance(allowed, str) else {allowed})
    return RelayEdge(
        source=str(source),
        target=str(target),
        edge_id=str(edge_id or ""),
        allowed_kinds=allowed,
        authorization_costs=value.get("authorization_costs", value.get("costs", {})),
        capacity=capacity,
    )


def graph_from_dict(value: RelayGraph | Mapping[str, Any] | Iterable[Mapping[str, Any]] | None) -> RelayGraph | None:
    if value is None:
        return None
    if isinstance(value, RelayGraph):
        return value
    if isinstance(value, Mapping):
        edges = value.get("edges", ())
        return RelayGraph(edges)
    if isinstance(value, Iterable):
        return RelayGraph(value)
    raise AdjudicationInputError("graph must be a RelayGraph, mapping, or iterable of edges")
