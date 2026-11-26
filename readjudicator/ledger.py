"""Mutable resource ledger used during exact allocation search."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .graph import RelayGraph, Route
from .model import Proof


@dataclass(frozen=True)
class Candidate:
    proof: Proof
    route: Route

    @property
    def sort_key(self) -> tuple[str, tuple[str, ...]]:
        return self.proof.proof_id, self.route.edge_ids


class Ledger:
    """Remaining proof uses, proof-bound authorizations, and edge capacities."""

    def __init__(self, proofs: dict[str, Proof], graph: RelayGraph | None = None) -> None:
        self.proofs = dict(proofs)
        self.uses_left: dict[str, int | None] = {pid: p.uses for pid, p in proofs.items()}
        self.auth_left: dict[str, dict[str, int | None]] = {
            pid: dict(p.authorizations) for pid, p in proofs.items()
        }
        self.edge_left: dict[str, int | None] = {}
        if graph is not None:
            for edge in graph.edges:
                self.edge_left[edge.edge_id] = edge.capacity

    def clone(self) -> "Ledger":
        other = Ledger.__new__(Ledger)
        other.proofs = self.proofs
        other.uses_left = dict(self.uses_left)
        other.auth_left = {pid: dict(auth) for pid, auth in self.auth_left.items()}
        other.edge_left = dict(self.edge_left)
        return other

    def snapshot(self) -> tuple[dict[str, int | None], dict[str, dict[str, int | None]], dict[str, int | None]]:
        return (
            dict(self.uses_left),
            {pid: dict(auth) for pid, auth in self.auth_left.items()},
            dict(self.edge_left),
        )

    def restore(self, snap: tuple[dict[str, int | None], dict[str, dict[str, int | None]], dict[str, int | None]]) -> None:
        uses, auth, edges = snap
        self.uses_left = dict(uses)
        self.auth_left = {pid: dict(values) for pid, values in auth.items()}
        self.edge_left = dict(edges)

    @staticmethod
    def _enough(remaining: int | None, amount: int) -> bool:
        return remaining is None or remaining >= amount

    def can_reserve(self, candidate: Candidate) -> bool:
        proof = candidate.proof
        if not self._enough(self.uses_left.get(proof.proof_id), 1):
            return False

        costs = candidate.route.total_authorization_costs()
        auth = self.auth_left.get(proof.proof_id, {})
        for key, amount in costs.items():
            if not self._enough(auth.get(key, 0), amount):
                return False

        for edge_id in candidate.route.edge_capacity_ids:
            if not self._enough(self.edge_left.get(edge_id), 1):
                return False
        return True

    def reserve(self, candidate: Candidate) -> bool:
        """Atomically reserve one proof slot and all route consumptions."""
        if not self.can_reserve(candidate):
            return False

        pid = candidate.proof.proof_id
        remaining_uses = self.uses_left[pid]
        if remaining_uses is not None:
            self.uses_left[pid] = remaining_uses - 1

        auth = self.auth_left[pid]
        for key, amount in candidate.route.total_authorization_costs().items():
            left = auth.get(key, 0)
            if left is not None:
                auth[key] = left - amount

        for edge_id in candidate.route.edge_capacity_ids:
            left = self.edge_left[edge_id]
            if left is not None:
                self.edge_left[edge_id] = left - 1
        return True

    def remaining_summary(self) -> dict[str, Any]:
        return {
            "proof_uses_left": dict(self.uses_left),
            "authorizations_left": {pid: dict(auth) for pid, auth in self.auth_left.items()},
            "edge_capacity_left": dict(self.edge_left),
        }
